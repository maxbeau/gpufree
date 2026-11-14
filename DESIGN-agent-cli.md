# gpufree CLI 优化设计（Agent 优先，第一性原理版）

- 状态：已实施（CLI 1.0.0，2026-11-14）；清理删除清单见 [gpufree-agent-cli-cleanup-report.md](../briefs/gpufree-agent-cli-cleanup-report.md)
- 日期：2026-11-14
- 范围：**只改 CLI**（`~/workspace/gpufree/` 的 Python 客户端与命令行）。平台（gpufree.cn 的接口、鉴权、计费、镜像行为）视为不可变约束，不在本设计内。
- 读者：CLI 的维护者与实施者（人或 Agent）。本文是三轮讨论的合并定稿，冲突处以本文为准。

---

## 0. 已被推翻的早期结论（先划掉，避免实施时复活）

| 早期提法 | 推翻理由 | 现行结论 |
|---|---|---|
| 平台提供个人访问令牌（PAT）/ API Key | 用户明确：平台不可改 | CLI 做 token 的唯一所有者 + 一次粘贴导入（§7.1） |
| 开机后自动重装 SSH 公钥（`--ssh-pubkey`） | 平台接口本身下发实例账号密码；公钥是 CLI 自造的状态，而开关机恰好冲掉它——自己造故障点再写补偿逻辑，不符合第一性 | **密码原生**：密钥体系整体删除，登录/验活/exec 全部走接口现取密码（§7.5） |
| 把当前 Horizon 的密钥通道立即切换 | 本设计只定 CLI 目标形态；现行运维通道何时切换由用户另行拍板 | 实施 CLI 时不自动触碰现行通道（§10） |

## 1. 背景：问题都有实证，不是猜测

本 CLI 于 2026-11-10 起在真实业务（Horizon H-Bot 训练运维）中高强度使用，撞到的问题均有日期可查：

| 日期 | 事件 | 暴露的问题 |
|---|---|---|
| 2026-11-10 | 提取 token：浏览器自动化读不到 cookie，最终靠用户在聊天里粘贴 `document.cookie` 两次才成功落盘 | 没有 Agent 可用的导入通道 |
| 2026-11-10 | 4090 开机吃 53523，CLI 先打印 `start requested` 且退出码 0 | 失败不可被脚本感知（此项当时已修：PlatformError + 非零退出码） |
| 2026-11-10 起 | `--when-available` 首次吃到 53523 即退出，运维手册明文改用自写「3 分钟 × 15 轮」循环 | 等待/重试语义不可靠，逻辑外溢到 Agent 侧 |
| 2026-11-10 起 | `fetch_target.py` 另行从接口取实例密码/端口，`gpf_hw.sh` + 华为枢纽 + sshpass 装公钥一整套外挂脚本 | CLI 缺 exec/验活能力，登录方式与状态管理被迫外置 |
| 2026-11-11 起 | 每次 stop/start 冲掉 `~/.ssh/authorized_keys`，须清 mux socket 后用密码重装公钥 | 自维护密钥状态的固有故障点 |
| 2026-11-13 | 浏览器重新登录使 CLI 的 refresh token 被轮换作废；滑块验证无法自动登录；靠用户贴 Cookie、手写 `token-state.json` 才恢复；轮询 cron 期间全停 | token 多持有者冲突 + 无分类诊断 + 无顺畅再导入 |
| 2026-11-13 | 用户贴入的 id_token 出现一处转写误差，靠 refresh 轮换才覆盖 | 人工粘贴需要校验与纠错设计 |
| 长期 | 训练因磁盘满「无日志静默死亡」；余额低位、关机 7 天销毁倒计时均靠外挂 cron 检查 | CLI 无阈值告警与成本/时间感知 |
| 长期 | CLI 未进 PATH，实际调用为 `cd ~/workspace/gpufree && python3 -m gpufree …`；`instances --json` 会带出 `ssh_password` | 安装形态与脱敏不一致 |

## 2. 不可变约束（平台侧，CLI 只能适应）

1. **鉴权只有浏览器会话一条路**：OIDC 风格，access token 约 7 天；refresh token 是 cookie 且**单次轮换**——任一持有者刷新后，其他持有者的副本即作废；登录有滑块验证，无法全自动。
2. **开机是即时成败**：无货直接返回 53523，没有预留、没有针对存量实例的排队；状态机异步（1 排队中 / 3 运行中 / 5 已关机）；GPU 只能在关机→开机时挂载，不能热加。
3. **实例登录凭证由接口下发**：`ssh_command`、`ssh_password` 随实例信息返回，随时可现取，端口变化后取到的即为最新值。镜像的根 overlay 在开关机时被冲掉，`authorized_keys` 不保留——但密码登录不依赖实例上的任何状态。
4. **create/clone 无幂等键**：重复调用可能重复建机、重复计费；clone 要求源实例已关机且有次数配额（53522）；release 不可逆；余额不足返回 42408。
5. **无 webhook、无事件推送**：一切状态变化只能靠轮询发现。
6. **接口源自前端逆向**：端点与字段来自官网 SPA 代码，平台改版可能静默改变字段——数据可能「看似正常实则错误」。

## 3. 第一性原理

### 3.1 Agent 对一个 CLI 的真实需求只有四个

1. **一次调用看清状态**（感知）
2. **一次调用达成目标状态**（行动）
3. **失败时明确知道下一步**：重试、等待、还是找人（决策）
4. **重试不会重复花钱、不会毁数据**（安全）

### 3.2 CLI 的定位：补偿层（anti-corruption layer）

平台的易变性（token 轮换、库存瞬时、异步状态、无幂等）不消失，就必须有人吸收。只有 CLI 能吸收：它把平台的全部不稳定性关在自己内部，对 Agent 只露出一个稳定、机器可读、可重试的契约。**凡是 Agent 侧出现的轮询循环、粘贴处理、拼装脚本，都是 CLI 缺位的证据**，应逐项收回 CLI。

### 3.3 由此推出的十条设计原则

1. **单一所有者**：轮换型凭证只能有一个刷新者。CLI 独占 token 链；浏览器会话与 CLI 不共用同一账号会话做刷新。
2. **派生优于存储**：凡能从接口现取的（实例密码、端口、库存、价格），一律现取现用，不落盘、不缓存为权威值。CLI 不自造平台会毁掉的状态（如公钥）。
3. **声明式优于命令式**：Agent 表达目标状态，CLI 内部跑「观察→决策→行动→验证」的调和循环。
4. **成功 = 端到端验过**：接口返回 200 不叫成功；开机成功要验到 SSH 可登、GPU 可见。
5. **失败必须可分类**：每个失败都带「是否可重试 / 建议动作」，退出码与之对应，Agent 不解析自然语言做决策。
6. **人只做不可替代的事**：滑块登录消不掉，就把那一次介入做到一步到位（一次粘贴、自动解析校验），其余全自动。
7. **机器契约优先**：一份稳定的 JSON 契约覆盖全部命令；人类可读输出是同一数据的渲染，不是另一套数据。
8. **花钱与销毁要有闸**：意图先记录、执行可预演（plan/dry-run）、非交互必须显式确认，靠本地日志实现客户端幂等。
9. **长尾有逃生舱**：精选命令覆盖 90% 场景；剩下 10% 用受控的原始接口直通兜底，Agent 不因 CLI 没包某接口而被堵死、也不需要另写探针脚本。
10. **薄**：无守护进程、无数据库、无第二套配置体系。CLI 的全部本地状态只有三个文件（§5.2）。不借一流 CLI 的体量，只借它们最锋利的单点（§12）。

## 4. 现状基线（已有的，不重做）

- selector（ID/UUID/名称，省略时取唯一实例）、`start/stop --wait`、`stock`、`watch`
- PlatformError 业务码翻译：53523（查库存/改无卡/克隆）、53524（先查状态）、成功后才打印确认语、失败 stderr + 退出码 1
- `instances` 人读白名单输出，`--wait` 返回过滤密码/token 字段
- token 文件 0600、XDG 路径、到期前 5 分钟主动刷新、并发刷新锁、401/43003 单次刷新重试
- create/clone/release 已实现（仅 mock 测试，未真机验证成功路径——实施时按 §11 补验）

## 5. 目标架构

### 5.1 分层

```
┌─────────────────────────────────────────────┐
│ Agent 面（稳定契约）                          │
│  auth · snapshot · instance ensure/plan      │
│  instance exec/ssh/tunnel · billing · events │
│  capabilities · schema · selftest · api      │
├─────────────────────────────────────────────┤
│ 调和与安全层                                  │
│  reconciler（调和循环）· journal（意图日志）    │
│  locks（实例操作锁）· redactor（统一脱敏）      │
│  envelope（统一信封/退出码）                    │
├─────────────────────────────────────────────┤
│ 平台客户端层（现有 GPUFeeClient）              │
│  token 独占管理 · 业务码 · 库存/状态轮询        │
│  原始接口方法（库函数，不直接暴露为命令）        │
└─────────────────────────────────────────────┘
```

### 5.2 本地状态（全部，位于 `$XDG_CONFIG_HOME/gpufree/`，权限 0600/0700）

| 文件 | 内容 | 说明 |
|---|---|---|
| `token-state.json` | 现行三键 token | 保留现有格式；写入必须「文件锁 + 原子替换」 |
| `token-state.json.bak` | 上一代 token | 仅一代；新导入验活失败自动回滚 |
| `journal.jsonl` | 操作意图与结果（append-only） | 只记操作类型、目标、参数哈希、执行者、结果码；**永不记密码/token/完整参数值中的敏感项** |
| `context.json` | 默认实例 selector 等少量默认值 | 可由 `instance use` 写、env 覆盖 |

不引入数据库、不引入守护进程。保活用系统 cron/systemd timer 调 `auth keepalive`（§7.1.4）。


## 6. 命令面设计

### 6.1 按对象组织（名词优先；实施后移除过渡命令）

用户的领域对象只有三个：账号、实例、目录。实施期间保留过渡命令，批次验收后按清理决策移除被名词命令替代的平铺入口。未纳入对象命令的只读接口通过 `api GET` 访问；client 库方法保留。

| 新命令 | 作用 | 取代/收编 |
|---|---|---|
| `gpufree auth status\|import\|keepalive\|snippet` | 鉴权全生命周期 | `check` `refresh` `login` `client-id` |
| `gpufree snapshot` | 一次调用的完整感知（§7.3） | `status` |
| `gpufree instance list\|get\|describe\|use` | 列表/摘要/详情/设默认 | `instances` `dashboard*` |
| `gpufree instance ensure running\|stopped` | 声明式调和（§7.4） | `start` `stop` `watch`（降为调试原语保留） |
| `gpufree instance plan running\|stopped` | 预演：步骤、成本、风险，不执行 | 新增（借 terraform plan） |
| `gpufree instance exec\|ssh\|tunnel` | 密码原生远程通道（§7.5） | 外部 ssh/sshpass/gpf_hw.sh |
| `gpufree instance create\|clone\|release` | 花钱/销毁三件套（§7.7 的闸） | `create` `clone` `release` |
| `gpufree instance events` | 统一时间线（§7.6） | 新增 |
| `gpufree billing balance\|history\|orders` | 账务 | `balance*` `orders` `transactions` `daily-bills` `vouchers` |
| `gpufree catalog stock\|products\|images` | 目录与库存 | `stock` `products` `hot` `gpu-names` `product-tags` `sku-prices` `images` |
| `gpufree api <METHOD> <path>` | 原始接口直通（§7.8） | 新增（借 gh api） |
| `gpufree capabilities` / `gpufree schema <命令>` | 自我描述（§7.8） | 新增 |
| `gpufree selftest` | 契约健康检查（§7.8） | 新增 |
| `gpufree version --check` | 版本与更新检查 | `--version` |

全局参数：`--json`（默认行为见 §7.2）、`--jq <expr>`、`--format <tmpl>`、`-q`、`--debug`、`--token-file`、`--profile`（预留多账号，首版可只实现默认）、以及每个参数对应的 `GPUFREE_*` 环境变量覆盖。

### 6.2 选择器与默认值

- selector 语义不变：ID / UUID / 完整名称；支持 `--filter status=running,product=L40` 形式的多实例筛选（借 docker/kubectl 的 filter）。
- `instance use <selector>` 把默认实例写入 `context.json`；优先级：命令行参数 > `GPUFREE_INSTANCE` > context > 唯一实例自动推断 > 报错并给出候选。

## 7. 详细设计

### 7.1 鉴权：token 独占 + 一次粘贴

#### 7.1.1 独占与并发
- 所有 token 读写走同一把文件锁（flock）；刷新在锁内完成并原子替换文件，杜绝「两个进程各刷一次、后写者作废先写者」。
- journal 记录每次刷新的时间与结果（不记值），`auth status` 可据此判断 token 链健康史。

#### 7.1.2 导入：`auth import`
接受三种输入格式，自动识别（借「把那一次介入做到极致」原则）：
1. 浏览器 `document.cookie` 原始输出（我们实际使用的就是这种）；
2. Cookie 请求头字符串；
3. token JSON（现有 login 流程的产物）。

输入源：`--stdin` / `--file <path>` / `--clipboard`（有 pbpaste/xclip 时）。
导入流程：解析 → 结构校验（JWT 可解码、iss 为 gpufree.cn、三键齐全；发现截断/转写错误要**指出具体哪一段不合法**，而不是笼统失败）→ 写入临时文件 → 立即 `refresh` 验活 → 成功才原子替换正式文件，旧文件转 `.bak`；验活失败自动回滚并报告分类原因。

#### 7.1.3 辅助生成：`auth snippet`
输出一段可存为浏览器书签的一行 JS：在 gpufree.cn 页面点击后，把 `document.cookie` 以导入所需格式复制到剪贴板。消灭手工开控制台、找字段、粘贴截断这三类错误。

#### 7.1.4 保活与状态分类：`auth keepalive` / `auth status`
- `keepalive`：供系统 cron 调用。执行一次带锁刷新 + `check`；成功静默退出 0；失败输出一行结构化告警并以对应退出码退出（见 §7.2.3）。替代现行外挂的 72 小时 cron 脚本逻辑（cron 本身可保留，只调这一条命令）。
- `auth status --json` 输出分类与下一步，分类枚举：
  `valid` / `expiring`（含剩余秒数）/ `refresh_rejected_rotated`（大概率被其他会话轮换）/ `refresh_rejected_expired` / `missing` / `network_error`。
  每类附 `action` 字段：一句可执行指令（如「在浏览器打开 gpufree.cn 登录后运行 auth snippet 并粘贴给 auth import --stdin」）。

#### 7.1.5 明确不做
不存账号密码、不绕滑块、不做自动登录。人的介入频率由 token 寿命决定（实测可达数周以上，前提是浏览器不再用同一账号刷新会话——在 `auth status` 的说明中明示这一条用户须知）。

### 7.2 统一机器契约

#### 7.2.1 信封
所有命令在 JSON 模式下输出同一信封（stdout 只出信封，进度与日志一律走 stderr）：

```json
{
  "ok": true,
  "schema": 1,
  "command": "instance.ensure",
  "data": { },
  "warnings": [ {"kind": "low_balance", "severity": "warn", "detail": "..."} ],
  "error": null,
  "meta": {"checked_at": "2026-11-14T12:00:00+08:00", "duration_ms": 1234, "cli_version": "x.y.z"}
}
```

失败时 `ok:false`，`error` 为：
```json
{"kind": "no_stock", "platform_code": "53523", "retryable": true, "action": "wait_and_retry | change_mode | ask_user", "message": "..."}
```

#### 7.2.2 输出模式与裁剪
- 模式判定：显式 `--json` 优先；否则 stdout 非 TTY 时自动 JSON、是 TTY 时人读。人读与 JSON 是同一份数据的两种渲染。
- `--jq <expr>`：内置过滤（实现可内置极简路径表达式或可选依赖 jq 引擎，二选一在实施时定；目标是让 Agent 不必把整包数据拉回再切）。
- `--format <tmpl>`：字段模板（借 docker --format）；`-q`：只输出关键标识（如实例 UUID、SSH 端点），供管道拼接。
- 字段名归一化：CLI 对外字段是自有稳定命名（如 `state: running|stopped|queued`），平台原始字段名只出现在映射层与 `api` 直通里。平台改名只改一处映射。
- 语言：JSON 键与枚举一律英文；人读渲染可用中文（现状风格）。
- 脱敏：统一 redactor 过滤一切输出路径（含 `--json`、错误对象、debug 日志）：键名命中 password/token/cookie/secret 即打码。裸原始输出仅 `api` 命令在显式 `--unsafe-raw` 时提供，且仍打码 token 类字段。**此条同时修复现状 `instances --json` 带出密码的问题。**

#### 7.2.3 退出码
| 码 | 含义 | Agent 动作 |
|---|---|---|
| 0 | 成功（含「已在目标状态」的幂等成功） | 继续 |
| 2 | 用法错误 | 改参数，不要重试 |
| 10 | 鉴权需人介入（refresh 已死/缺失） | 按 error.action 找人 |
| 11 | 无库存超时（retryable） | 等待后重试或降级 nogpu |
| 12 | 余额不足（42408） | 停下找人充值 |
| 13 | 状态冲突（53524 等） | 先 snapshot 再决策 |
| 14 | 网络错误（retryable） | 退避重试 |
| 15 | 端到端验证失败（状态对但 SSH/GPU 不通） | 看 describe/debug，不要盲目重开 |
| 16 | 被其他进程的操作锁占用 | 读 journal 看进度，等待 |
| 17 | 契约漂移（selftest/解析发现未知结构） | 停下报告，不要按猜测数据行动 |

其余平台业务码：保留原始 code 于 `error.platform_code`，退出码归入最接近的一类，未知归 1 并在 message 中原样带出。

#### 7.2.4 环境变量
每个全局参数有 `GPUFREE_*` 对应项（token 文件、默认实例、JSON 开关、超时）。方便 Agent/CI 注入，不写进任何仓库文件。

### 7.3 Snapshot：一次感知

`gpufree snapshot` 并行拉取（沿用现有 get_status 的并发实现）并输出已算好的结论，而非原始响应堆叠：

- `auth`：分类 + 剩余寿命（§7.1.4）
- `balance`：余额数字（元）、当前总燃烧率（¥/h)、按当前燃烧率可撑小时数
- `instances[]`：归一化字段——id/uuid/名称/产品/机房/`state`/`start_mode`/计费/SSH 端点（host/port/user，**不含密码**）/根盘使用率/数据盘可用空间（可经 exec 补充，见下）/`release_at` 与剩余秒数
- `stock_for`：每个实例对应产品在其机房的实时库存
- `warnings[]`：CLI 直接算好的阈值判断（阈值可配，给默认值）：
  - 余额 < 24 小时燃烧成本 → `low_balance`
  - 根盘或数据盘使用率 > 85% → `disk_high`（盘满曾导致训练无日志静默死亡）
  - 实例处于 running+gpu 且 GPU 利用率为 0 持续超过阈值 → `gpu_idle`（需 exec 采样 `nvidia-smi`，采样失败则该项标 `unknown`，不报错）
  - 关机实例 `release_at` < 48 小时 → `release_soon`
  - token 剩余 < 48 小时 → `auth_expiring`
- 每次 snapshot 的精简结果落一份 `last-snapshot.json`（同目录，含时间戳）：网络/鉴权故障时 Agent 仍可读到「截至何时的最后已知状态」，且 meta 里必须标注 `stale:true` 与年龄，不许冒充实时。

### 7.4 Ensure：声明式调和

#### 7.4.1 语义
```
gpufree instance ensure running  [--mode gpu|nogpu] [--deadline 45m]
                                 [--verify ssh,gpu,disk] [--ttl 2h]
                                 [--max-hourly-cost 3.00] [--plan-only]
gpufree instance ensure stopped  [--deadline 5m]
```

- 已在目标状态且验证通过 → 幂等成功（退出 0，`data.changed:false`）。
- 处于中间态（排队中、正在切换）→ 等待收敛，不重复下单。
- 模式不符（如 running+nogpu 而目标 gpu）→ 这是破坏性切换（必须先关机），默认拒绝并在 error 中说明，显式 `--allow-restart` 才执行——把风险决策留给调用方明示。

#### 7.4.2 调和循环（内部实现，取代一切外部轮询脚本）
```
读状态 → 已达标? → 验证(§7.4.3) → 成功返回
   ↓ 未达标
查库存（gpu 模式）→ 有货 → 下单 start → 等状态收敛 → 验证
   ↓ 无货/53523
在 deadline 内按间隔（默认 30s，带抖动）重试查库存+下单
   ↓ 53524 等状态冲突
重读状态再决策（不盲重试）
   ↓ 超过 deadline
以退出码 11 失败，error 带最后一次库存与状态
```
- 等待参数只有一个 `--deadline`（废除现状一个 `--wait-timeout` 复用于两段等待的含混）。
- 进度以 JSON Lines 打到 stderr（每次状态变化一条），stdout 保持单一信封（借 NDJSON 事件流，脚本可流式消费）。
- 收到 SIGINT：只取消本地等待，不撤销已发出的平台操作，并在输出中说明平台侧当前状态。

#### 7.4.3 端到端验证（成功定义）
状态到 running 后依次验证，任一步失败以退出码 15 返回并指出卡在哪一步：
1. TCP 端口可达（端点取自最新实例信息）；
2. 密码登录成功（§7.5 的通道）并执行探针命令；
3. `--verify` 含 gpu 时：`nvidia-smi` 返回型号与显存，与产品名核对；
4. 含 disk 时：`df` 返回根盘/数据盘可用空间，写入结果。
网络路径不通（如沙箱直连被代理拦截）与实例本身故障要分开报告：error.kind 分别为 `verify_network_unreachable` 与 `verify_failed`——前者提示走 `--via`（§7.5.3），不要误导调用方去重启实例。

#### 7.4.4 Plan 模式
`instance plan running ...` 或 `ensure --plan-only`：只读输出将执行的步骤序列、每步预计耗时范围、成本变化（¥/h 前后）、风险提示（需重启、将冲掉实例内未持久化状态等），不发任何写请求（借 terraform plan）。Agent 在真正花钱/重启前可先把 plan 给人看。

#### 7.4.5 TTL 租约
`--ttl` 在调和成功后，通过操作系统调度（systemd-run 优先、cron 兜底）注册一条到期执行的 `instance ensure stopped`，并在 journal 记下租约 id 与到期时间；`instance events`/describe 可见当前租约。到期任务自带同一套锁与幂等，重复触发无害。不引入守护进程。

### 7.5 远程通道：密码原生（本设计的核心纠正）

#### 7.5.1 原则
实例密码是平台接口的派生值：**现取、内存内使用、一次调用即弃**。CLI 不生成、不保存、不安装任何 SSH 密钥；平台冲掉 `authorized_keys` 这件事从此与本 CLI 无关。

#### 7.5.2 命令
- `gpufree instance exec <selector> -- <命令...>`：Agent 主力。现取端点+密码 → 建立 SSH → 执行 → 透传远程 stdout/stderr 与退出码（CLI 自身错误仍走信封/stderr，不与远程输出混淆）。支持 `--timeout`、`--stdin` 管道输入。
- `gpufree instance ssh <selector>`：交互登录（TTY 直通）。
- `gpufree instance tunnel <selector> -L <本地端口>:<目标>:<端口>`：端口转发。
- 三者共用同一连接层。长会话复用在单次 CLI 调用内解决（exec 可一次传多条命令或脚本文件 `--script <path>`），不追求跨调用的连接保活——那正是旧 mux 方案复杂度的来源。

#### 7.5.3 实现约束
- 优先进程内 SSH 库（如 paramiko）直连，避免 `sshpass` 把密码暴露在进程参数（ps 可见）里；若运行环境只有系统 ssh，退化为 askpass + 环境/fd 传密码，**禁止**密码出现在 argv。
- 密码永不进入：stdout/stderr、journal、debug 日志、崩溃堆栈（异常对象要过 redactor）。
- `--via <host>`：直连网络不通时经跳板建立连接（借现有华为枢纽场景）。跳板只解决网络可达性，与登录方式无关；跳板自身的登录走其既有方式，不由本 CLI 管理。
- host key：首次连接记录指纹到本地状态文件，后续变更要显式报警（实例重建会换指纹，届时提示确认后更新，不静默接受）。

### 7.6 可观测性三层（借 kubectl get/describe/events）

- `instance get`：一行一实例的摘要表/JSON，供管道与快速判断。
- `instance describe <selector>`：单个实例的完整叙事——归一化状态、计费与累计成本估算、磁盘、SSH 端点、当前租约、最近 journal 操作、平台侧近期账单事件、warnings。这是一切排障的第一入口。
- `instance events [--since 24h]`：把本地 journal 与平台订单/交易流水按时间合并为一条时间线（来源字段标明 local/platform），回答「谁在什么时候开/关过机、余额为什么掉」。
- `--debug`（全局）：每次平台请求的 method/path（不含凭证）、耗时、重试次数、业务码，以脱敏形式打到 stderr；`--trace-file <path>` 另存一份供事后复盘。

### 7.7 花钱与销毁的安全闸

- **意图日志先行**：create/clone/release/ensure 的写操作，执行前先向 journal 追加一条 intent（操作、目标 selector、参数哈希、执行者标识：主机名/PID/可选的会话标识 env），执行后追加 result。进程崩溃后，下一次调用先检查未闭合的 intent：对 create/clone，先用实例列表按名称/时间对账，同名实例在意图窗口内已出现则认定已执行、拒绝重复下单并报告。
- **客户端幂等键**：create/clone 接受 `--idempotency-key`（Agent 生成 UUID）；同 key 在 journal 中已有成功结果则直接返回该结果。无 key 时以「名称+参数哈希+时间窗」做弱去重并在输出中注明。
- **预演与确认**：三件套都支持 `--dry-run` 输出将发的请求（脱敏）与费用预估（¥/h 及首小时成本）；非 TTY 环境必须显式 `--yes` 才真正执行；TTY 环境的确认提示必须写明目标、费用与不可逆性。release 沿用现有「UUID 前 5 位确认」并叠加 `--yes`。
- **实例操作锁**：以 selector 为粒度的文件锁串行化写操作；拿不到锁时以退出码 16 返回，并在 error 中给出持锁者的 journal 进度（谁、正在做什么、已多久）。解决主聊/侧聊/cron 多 Agent 并发操作同一实例的竞态。
- **费用上限**：全局可选 `--max-hourly-cost`（或 env），create/clone/ensure 的目标形态超过上限直接拒绝（退出码 12 同类处理并注明是本地上限而非平台余额）。

### 7.8 逃生舱、自我描述与抗漂移

- **`api` 直通**：`gpufree api GET /api/v1/... [--body <json>]`，复用 token 管理、退避重试与 redactor；业务码非 200 时走统一信封与退出码。平台新增接口时 Agent 立即可用，不等 CLI 发版；历史上取密码、 probe 克隆接口都属于此类场景。
- **`capabilities`**：输出机器可读的命令清单（名称、参数、是否写操作、所需鉴权），Agent 无须读源码或 `--help` 文本即可发现能力。
- **`schema <命令>`**：输出该命令 JSON 信封中 `data` 的 JSON Schema（含枚举与单位）。契约版本随信封 `schema` 字段演进，破坏性变更必须升版本。
- **`selftest`**：只读契约检查——逐个探活 CLI 依赖的平台接口，校验响应关键字段存在且类型符合映射表（状态码集合、价格单位、实例字段）；输出健康报告（每项 ok/drifted/down）。解析层遇到未知状态码/缺失字段时**显式标 unknown 并报 17**，禁止猜测填充（现状 stock 曾出现机房库存数分钟内变化，须与「结构漂移」严格区分：数值变化正常，结构变化才报警）。
- **shell 补全**：`gpufree completion bash|zsh|fish` 生成脚本（借 gh/kubectl）；`--help` 每个子命令带至少一个实例示例；未知命令给出 did-you-mean（如 strat → start）。

### 7.9 安装与版本

- 提供可直接安装的发行形态（pipx/uv 工具安装或单文件可执行），`gpufree` 必须在 PATH 上；废除「进仓库目录 `python3 -m gpufree`」的用法（保留为开发调试方式并写明）。
- `gpufree version` 输出版本与安装路径；`--check` 只读检查是否有新版本（比对仓库/包索引），仅提示不自动升级。

## 8. 明确不做的事

- 不尝试绕滑块/自动登录；不存平台账号密码。
- 不做守护进程、Web 服务、数据库；不做多账号的复杂 profile 体系（只留参数位）。
- 不把项目级工作流塞进 CLI 核心（如训练任务管理）；项目脚本应调用 CLI，而非反过来。需要扩展时用别名（config 中定义命令别名）解决，不做插件系统（单用户规模下插件机制是过度设计，列为远期可选）。
- 不借：AWS CLI 的参数海洋、kubectl 的 YAML 配置体系、Terraform 的重状态文件。这些与「单账号、少量实例、薄补偿层」的定位冲突。

## 9. 可删除的外部脚本（CLI 功能落地后的回收清单）

| 外部现状 | 被谁取代 | 备注 |
|---|---|---|
| 自写抢卡循环（3 分钟 × 15 轮） | `ensure` 的调和循环（§7.4.2） | holoboxing-ops 手册中的对应纪律同步改写 |
| token 保活 cron 的自写逻辑 | cron 只调 `auth keepalive` | cron 条目保留，内容简化 |
| `fetch_target.py`（取密码/端口） | `exec/ssh/describe` 内部现取（§7.5） | 密码不再经过任何脚本文件中转 |
| `gpf_hw.sh`、华为侧临时密钥、mux socket 管理 | 密码原生通道；网络不通时 `--via` | **注意**：此回收涉及现行 Horizon 运维通道，按 §0 的口径，切换时点由用户另行拍板，不随 CLI 实施自动发生 |
| Agent 侧的余额/磁盘/销毁倒计时轮询 | `snapshot` + warnings（§7.3） | 阈值判断收回 CLI |

## 10. 实施路线（按收益/风险排序）

### P0 — 契约与鉴权（纯本地、零平台写操作、风险最低）
1. 统一信封 + 退出码表 + redactor（含修复 `instances --json` 密码泄漏）
2. `auth import`（三格式）+ `auth status` 分类 + 锁/原子写/一代备份；`auth snippet`
3. `snapshot`（含 warnings 与 last-snapshot 落盘）
4. 打包进 PATH + `version`
- 验收：见 §11 的 P0 用例全过；旧命令行为除输出信封外不回退。

### P1 — 调和与远程通道（核心价值，需真机验）
5. `instance` 名词重构 + context 默认实例（老命令别名）
6. `ensure`（含 plan、deadline、NDJSON 进度、退出码 11/13/15 区分）+ 操作锁
7. `exec/ssh/tunnel`（密码原生，含 `--via` 与 host key 指纹管理）
- 验收：真机完成一次「关机 → ensure running（gpu）→ 验 GPU → exec 跑命令 → ensure stopped」全闭环；中途人为制造无货（选无货机型）验证 deadline 与退出码。

### P2 — 安全闸与长尾
8. journal 意图日志 + 幂等键 + create/clone/release 的 dry-run/费用预估/`--yes`（并在此阶段补三件套的真机小规格验证：建一台最小实例、克隆、释放，各一次，经用户批准后执行）
9. `api` 直通 + `capabilities`/`schema` + 补全脚本
10. `describe`/`events` 合并视图、TTL 租约、费用上限
11. `selftest` 契约检查
- 验收：重复执行同一 `--idempotency-key` 的 clone 不产生第二台实例；selftest 能识别人为篡改的字段映射（用 fixture 模拟）。

## 11. 测试计划

- **单元/契约测试**（扩展现有 tests/）：信封结构、退出码映射、redactor（三类敏感键、嵌套结构）、import 三格式解析（含截断/错字样本，就用 2026-11-13 的真实失败形态造 fixture）、调和循环状态机（mock 平台：无货→有货、53524 中间态、验证失败分支）、journal 去重。
- **只读真机测试**：snapshot/auth status/selftest/api GET 在真实账号上跑通且输出脱敏抽查无泄漏。
- **写操作真机测试**：仅在用户批准的窗口内做（开机/关机属按量计费、create/clone/release 产生真实费用与配额消耗），每次测后复核余额与实例列表，并在 journal 留痕。
- **回归基线**：现有 test_cli/test_client/test_lifecycle/test_p0 全量保持通过；命令别名期至少覆盖一个版本。

## 12. 附录 A：借鉴来源对照（只借单点，不借体量）

| 借鉴点 | 来源 | 在本设计的落点 |
|---|---|---|
| `api` 原始直通、别名 | gh | §7.8、§8 |
| `--jq` 输出过滤 | gh | §7.2.2 |
| get/describe/events 分层、`--for=condition` 等待 | kubectl | §7.4、§7.6 |
| context/默认对象 | kubectl、docker context | §6.2 |
| plan 先于 apply | terraform | §7.4.4、§7.7 |
| inspect 的完整叙事、-q 静默输出 | docker | §7.2.2、§7.6 |
| waiter（服务侧状态等待器） | aws cli | §7.4.2（收敛为 ensure 内部实现） |
| dry-run 一等公民 | rclone、kubectl | §7.7 |
| 结构化事件流/日志跟随 | stripe cli、flyctl | §7.4.2 的 NDJSON 进度 |
| 自我更新检查 | tailscale、flyctl | §7.9 |

## 13. 附录 B：未决问题（实施前需要一个决定即可，不阻塞 P0）

1. `--jq` 的实现选型：内置极简实现 vs 依赖外部 jq 二进制——倾向内置路径/过滤子集，避免给 Agent 环境添依赖。
2. SSH 库选型：引入 paramiko 依赖 vs 纯系统 ssh + askpass——倾向 paramiko（密码不进 argv 的要求更易达标），但需确认目标运行环境可安装。
3. 别名淘汰节奏：老命令保留几个版本后移除——倾向至少保留到 Horizon 赛事结束（2027-01-07），避免运维脚本在赛事期被迫迁移。
4. TTL 租约的调度载体在各运行环境（沙箱/华为/RackNerd）的可用性不一致——实施 P2 时按环境探测，注册失败则明确报错，不假装已设。
