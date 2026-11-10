# gpufree - 算力自由 API Client

A Python client and CLI for [gpufree.cn](https://www.gpufree.cn). It handles
browser-style token refresh, account balance queries, instance listing, and
product browsing.

## Installation

Python 3.10 or newer is required. A virtual environment is the preferred
development setup:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --editable .
```

For an isolated command available outside the checkout, use `pipx`:

```bash
pipx install .
```

On a PEP 668-managed system where neither `pipx` nor the `venv` module is
available, a user-site-only fallback is:

```bash
python3 -m pip install --user --break-system-packages --no-deps --editable .
```

These commands create the `gpufree` executable through the entry point declared
in `pyproject.toml`. For user-site installs, ensure the scripts directory,
commonly `~/.local/bin`, is on `PATH`.

## Quick start

```bash
# Show balance, instances, user information, and dashboard data
gpufree status

# Query individual resources
gpufree balance
gpufree instances
gpufree instance-left-copy

# Manual control by ID, UUID, or exact name (omit selector for a sole instance)
gpufree start my-instance --mode gpu --wait --wait-timeout 120
gpufree stop my-instance --wait

# Inspect or refresh the current session
gpufree check
gpufree refresh

# Login when the refresh token is unavailable or invalid
gpufree login

# Show the installed version
gpufree --version
```

## Use as a Python library

```python
from gpufree import GPUFeeClient

client = GPUFeeClient()                       # uses the XDG config directory
client = GPUFeeClient(token_file="...")       # custom token path
client = GPUFeeClient(client_id="...")        # override reported client_id

status = client.get_status()
print(status["balance_summary"]["balance_yuan"])  # "165.71"

user = client.get_userinfo()
balance = client.get_balance_overview()
instances = client.list_instances()
products = client.get_hot_products()
```

`get_status()` fetches its five independent resources concurrently. It returns
the raw responses together with machine-readable `balance_summary` strings and
a compact `instances_summary`.

Manual control methods are `start_instance(id, uuid, start_mode="gpu")`,
`stop_instance(id, uuid)`, `resolve_instance(selector)`, and
`start_instance_by_selector()` / `stop_instance_by_selector()`. Start also accepts
`start_mode="nogpu"`. Selector methods accept `wait=True` and `wait_timeout=120`;
waiting polls every five seconds and returns
`{"response": API_response, "instance": final_item}` instead of the raw response.

### Available API methods

| Category | Methods |
| --- | --- |
| User | `get_userinfo()`, `get_unread_count()`, `get_messages()`, `get_invitation_code()` |
| Balance | `get_balance()`, `get_balance_overview()`, `get_balance_history()`, `get_orders()`, `get_transactions()`, `get_daily_bills()`, `get_recharge_state()`, `get_vouchers()` |
| Instances | `list_instances()`, `get_instance_status_tags()`, `get_instance_ide_res()`, `get_instance_left_copy()` |
| Lifecycle | `create_instance(...)`, `clone_instance_by_selector(...)`, `release_instance(id, uuid)`, `release_instance_by_selector(...)` |
| Dashboard | `get_dashboard_instances()`, `get_dashboard_storage()` |
| Products | `list_products()`, `get_hot_products()`, `list_gpu_names()`, `get_product_tags()`, `get_sku_price()` |
| Other | `list_ssh_keys()`, `list_file_storage()`, `list_public_images()` |
| Composite | `get_status()` |
| Auth | `refresh_token()`, `check_expiry()`, `login_interactive()` |

## Token management

By default, tokens are stored with `0600` permissions at:

```text
$XDG_CONFIG_HOME/gpufree/token-state.json
```

When `XDG_CONFIG_HOME` is unset, the path is
`~/.config/gpufree/token-state.json`. Use `--token-file PATH` or the
`token_file` constructor argument to select another location.

Upgrades from the original repository-local layout are automatic: if the new
default file does not exist and `token-state.json` is found beside the project
package, the client atomically copies it to the new location. The legacy file
is left untouched so it can be removed after the migrated login is verified.

The token state contains credentials similar to:

```json
{
  "access_token": "<JWT, normally valid for about 7 days>",
  "refresh_token": "<browser cookie used for authentication refresh>",
  "id_token": "<JWT containing client_id metadata>",
  "token_type": "Bearer",
  "expires_in": 604800,
  "scope": "openid profile offline_access email phone user_info"
}
```

- `gpufree check` inspects access-token and ID-token expiry.
- `gpufree refresh` rotates tokens immediately through the cookie endpoint.
- `gpufree login` obtains a new browser session interactively.
- `gpufree client-id` reports the active client ID and its public source.

Before protected API calls, the client refreshes when either JWT is within five
minutes of expiry. Concurrent requests share a refresh lock, so one rotated
refresh token is not submitted multiple times. A 401 or API code 43003 triggers
at most one refresh and request retry.

The OAuth client ID is resolved in this order:

1. Explicit `client_id` constructor or `--client-id` CLI argument.
2. The stored `id_token` or `access_token` JWT.
3. The built-in client ID from the service's SPA registration.

Cookie-based token refresh does not use a client secret.

## Automation and cron

Balance values contain no thousands separators, so they can be passed to tools
such as `jq` and `bc`.

```bash
# Hourly status log
0 * * * * $HOME/.local/bin/gpufree status >> $HOME/gpufree-status.log 2>&1

# Alert when the balance drops below CNY 100
*/30 * * * * STATUS=$($HOME/.local/bin/gpufree status 2>/dev/null) && BALANCE=$(echo "$STATUS" | jq -er '.balance_summary.balance_yuan | tonumber') && [ "$(echo "$BALANCE < 100" | bc)" = 1 ] && echo "Low balance: CNY $BALANCE" | mail -s "GPUFee Alert" you@example.com
```

## Development and tests

The test suite uses only the Python standard library:

```bash
python3 -m unittest discover -s tests -v
```

The tests cover proactive and concurrent refresh, missing access-token recovery,
401 and 43003 retries, timeout backoff, invalid JSON, atomic token persistence,
balance formatting, composite status concurrency, CLI routing, and mocked
lifecycle payloads, prechecks, confirmations, and platform failures. Tests never
call the live gpufree API or create, clone, or release real instances.

```text
gpufree/
├── gpufree/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   └── client.py
├── tests/
│   ├── __init__.py
│   ├── test_cli.py
│   ├── test_client.py
│   ├── test_lifecycle.py
│   └── test_p0.py
├── .gitignore
├── pyproject.toml
└── README.md
```

## Known limitation

The service's slide captcha prevents fully automated login. `gpufree login`
therefore requires copying token JSON from a dedicated private browser window.
Close that window after copying the tokens so the browser does not rotate the
same refresh token independently.

## Stock, watch, and instance control

`gpufree instances` now shows a readable section per instance: name, UUID,
product, datacenter, status, start mode, billing type, SSH command, optional root
filesystem usage, and optional release time with remaining duration (or 已过期).
Human output uses an explicit field allowlist and excludes passwords and tokens.
`gpufree instances --json` preserves the original raw JSON response, including
any sensitive fields returned by the platform; handle that output accordingly.
`gpufree status` remains JSON; each `instances_summary` entry also includes
`status`, `status_text`, `start_mode`, and `release_time` when present.

```bash
# Case-insensitive product-name substring; omit query to show all products
gpufree stock 4090
gpufree stock L40 --available
gpufree stock --json

# Watch until running (default) or stopped; print only state changes
gpufree watch my-instance
gpufree watch my-instance --until stopped --interval 10 --timeout 300

# Wait for GPU stock in the instance's datacenter, then start
gpufree start my-instance --when-available --wait-timeout 1800 --wait

# Start without a GPU if stock is zero, or a GPU start returns 53523
gpufree start my-instance --fallback nogpu

# Combined: wait first, fall back only at the stock deadline or on 53523
gpufree start my-instance --when-available --fallback nogpu --wait-timeout 600
```

Stock displays prices converted from cents to `¥x.xx/h`, total available nodes,
and positive per-datacenter counts (`dc<id>:<left>`; `0` when none).
`--available` retains only products with a positive node total; `--json` returns
an array of matching original product dictionaries.

Watch defaults to a 10-second interval and unlimited timeout (`--timeout 0`).
A positive timeout exits with code 1 and the last observed state; disappearance
from the instance list is also an error. Each change includes a local timestamp.

Stock-aware starts match the instance's product name and prefer matching
`datacenter_id` SKUs, even when their stock is zero. Only when no such SKU exists
do they use the product total. `--when-available` checks every 30 seconds, bounded
by `--wait-timeout` (default 120 seconds). A missing product match is an error.
`--fallback nogpu` makes at most one CPU-only start attempt and explicitly reports
the downgrade. Both flags are ignored with an explanation under `--mode nogpu`.
Stock waiting and the subsequent optional state waiting each use the configured
`--wait-timeout` independently.

| Code | Meaning / action |
| --- | --- |
| Instance status 1 | 排队中 (queued) |
| Instance status 3 | 运行中 (running) |
| Instance status 5 | 已关机 (stopped) |
| Billing type 0 | 按量付费 (pay as you go) |
| Control code 200 / `"200"` | Request succeeded; optional state waiting may begin |
| Control code 53523 | Datacenter GPU stock insufficient: check `gpufree stock`, use `--mode nogpu`, or clone/create through the website |
| Control code 53524 | Operation unavailable in the current state: check `gpufree instances` |
| Other control codes | Original code and platform message are reported without interpreting them |

Unknown instance status and billing codes are displayed as their original values.
Failed start/stop requests exit with code 1 on stderr, without success confirmation
or entering state waiting. Control confirmations omit password/token fields,
including from an instance returned by `--wait`. In Python they raise `gpufree.PlatformError`, retaining
`.code` and the original `.msg`. Other API methods keep their existing response
passthrough behavior. Stock helpers are `stock_products()`, `product_stock()`,
`gpu_stock_for_instance()`, and `wait_for_gpu_stock()`; `watch_instance()` shares
the control methods' polling implementation and accepts a `progress` callback.

## Create, clone, and release

```bash
# Required: SKU code, datacenter ID, and image ID; count defaults to 1
gpufree create --sku CODE --dc 1 --image 123
gpufree create --sku CODE --dc 1 --image 123 --count 1 --mode nogpu \
  --charge-type 0 --expand-disk-gb 0 --duration-type "" --duration-count 1

# Source must be stopped; cloning excludes data unless --with-data is given
gpufree stop my-instance --wait
gpufree clone my-instance
gpufree clone my-instance --name "my copy" --with-data --expand-disk-gb 100

# Release requires the target UUID's first five characters, or its full UUID
# Example target UUID: abcde-0123456789
gpufree release my-instance --confirm abcde
```

Create defaults to GPU mode, pay as you go (`--charge-type 0`), no disk expansion,
empty duration type, and duration count 1. Mode accepts `gpu` or `nogpu`; charge
type accepts 0 (pay as you go) or 1 (duration billing); count must be at least 1.
Use `gpufree products` and `gpufree images` to inspect product and image data.

Clone and release accept an instance ID, UUID, or exact name; omitting the
selector requires exactly one instance. Clone checks status 5 (stopped) before
querying `/api/v1/jupyter/left_copy`, then blocks if `clone_count_left <= 0`.
An unavailable quota field does not block the request; platform errors still
apply. Clone defaults to `<source nick_name or UUID> 副本`, the source SKU,
datacenter, and requested amount, pay as you go, duration type `day`, duration
count 1, and no data copy. Source count uses `req_amount`, falling back to the
instance list's `req_gpu_amount`. The website also checks expanded disk size against
the source data disk; choose an appropriate size when cloning.

Release permanently removes the selected instance. Confirmation is
noninteractive: the first five UUID characters are case-insensitive; the full
UUID is also accepted case-insensitively. Missing confirmation exits 1 before
any API request, and a mismatch exits 1 after target lookup without sending
DELETE. All three commands print filtered JSON on success and report
RuntimeError/ValueError (including platform failures) on stderr with exit 1.
Password/token filtering reuses the start/stop control output rules.

The Python methods use the same response checks as start/stop:

```python
created = client.create_instance(
    product_sku_code="CODE", data_center_id=1, image_id=123,
    charge_type=0, req_amount=1, start_model="gpu", expand_data_disk_gb=0,
    duration_type="", duration_count=1,
)
cloned = client.clone_instance_by_selector(
    "my-instance", nickname="my copy", clone_data=False,
    charge_type=0, expand_data_disk_gb=0, duration_type="day", duration_count=1,
    # Optional product_sku_code, data_center_id, req_amount override source values.
)
```

Library release methods (`release_instance(id, uuid)` and
`release_instance_by_selector(selector=None)`) do not prompt or require a
confirmation argument: the caller is responsible for confirmation. Raw library
responses are not filtered. A failed control response raises `PlatformError`
with the original `.code` and `.msg`, including these additional hints:

| Code | Hint |
| --- | --- |
| 53522 | 源实例需处于关机状态；克隆前请先 stop 源实例 |
| 42408 | 余额不足，请先充值 |

Endpoints and payload fields come from the official website's frontend code:
create uses `POST /api/v1/inferring-api/webide` (no trailing slash); clone and
release use `PATCH` and `DELETE /api/v1/inferring-api/webide/` (with trailing
slash). `expand_data_disk_gb` / `--expand-disk-gb` are converted to bytes using
`GB * 1024**3`, assuming the frontend's `GB2Bytes` implements GiB.

Verification level: the source-stopped prerequisite was verified in a prior
live check on 2026-11-10 (running-source clone returned 53522), as recorded in
the supplied interface findings. **Successful create/clone/release paths have
not been tested on real instances.** This implementation was tested only with
mocks; the user waived live testing on 2026-11-10. For the first real use, try a
small configuration first; creation/cloning can incur charges and consume quota.
