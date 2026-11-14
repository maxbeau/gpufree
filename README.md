# gpufree

Python client and command line interface for gpufree.cn. Python 3.10+ is
required; the CLI uses only the standard library.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --editable .
```

## Commands

```text
gpufree snapshot
gpufree auth status|import|keepalive|snippet
gpufree instance list|get|describe|use|ensure|plan|exec|ssh|tunnel|events|create|clone|release
gpufree billing balance|history|orders
gpufree catalog stock|products|images
gpufree api GET|POST /path
gpufree capabilities
gpufree schema COMMAND
gpufree selftest
gpufree version [--check]
```

Global options include `--token-file`, `--instance`, `--json`, `--jq`,
`--format`, and `-q`. `GPUFREE_TOKEN_FILE`, `GPUFREE_INSTANCE`, and
`GPUFREE_JSON=1` provide defaults. `--jq` accepts dot paths and numeric list
indexes, such as `data.instances.0.state`; use system `jq` for filters. The
`--format` option substitutes `{field}` or `{dot.path}` placeholders.

## Output and exit codes

JSON output uses a schema 1 envelope with `ok`, `command`, `data`, `warnings`,
`error`, and `meta`. Credentials and password-like fields are redacted. Progress
for long-running operations goes to stderr. Exit codes are 0 success, 2 usage,
10 authentication, 11 no stock/deadline, 12 insufficient balance, 13 state
conflict, 14 network, 15 verification, 16 operation lock, and 17 API contract
drift; other failures use 1.

## Authentication

Open gpufree.cn in a browser, run `gpufree auth snippet`, and click the
bookmarklet on the site to copy the session cookie. Paste it into:

```bash
gpufree auth import --stdin
```

The import is verified by default; use `--no-verify` only when an offline import
is intended. `gpufree auth status` classifies the local token state and
`gpufree auth keepalive` is the scheduled refresh entry point. Do not refresh
the same browser session independently while the CLI owns its rotating token.
Tokens live in `$XDG_CONFIG_HOME/gpufree/token-state.json` (default
`~/.config/gpufree/token-state.json`) with restrictive permissions.

## Instance operations

```bash
gpufree snapshot --json
gpufree instance use my-instance
gpufree instance ensure running --mode gpu --deadline 45m --verify ssh,gpu
gpufree instance exec my-instance -- nvidia-smi
gpufree instance ensure stopped
```

`ensure` converges to a requested state and supports `--plan-only`, `--ttl`,
`--max-hourly-cost`, and `--allow-restart`. Remote execution uses system
OpenSSH. Create, clone, and release accept `--dry-run`; non-interactive writes
require `--yes`, and release also requires UUID confirmation.

## Migration from flat commands

| Old command | Current command |
| --- | --- |
| `status` | `snapshot` |
| `balance`, `balance-detail` | `billing balance` |
| `balance-history`, `transactions`, `daily-bills`, `vouchers` | `billing history` |
| `orders` | `billing orders` |
| `instances`, `dashboard`, `dashboard-storage`, `instance-tags`, `instance-ide`, `instance-left-copy` | `instance list`, `instance describe` |
| `start`, `stop`, `watch` | `instance ensure running|stopped` |
| `create`, `clone`, `release` | `instance create`, `instance clone`, `instance release` |
| `stock` | `catalog stock` |
| `products`, `hot`, `gpu-names`, `product-tags`, `sku-prices` | `catalog products` |
| `images` | `catalog images` |
| `check`, `refresh`, `login`, `client-id` | `auth status`, `auth keepalive`, `auth import`, `auth status` |
| `userinfo`, `messages`, `message-list`, `invitation`, `ssh-keys`, `storage`, `recharge-state` | `api GET /endpoint` (use `schema`/`api` as needed) |

The former flat commands have been removed. The Python `GPUFeeClient` methods
remain available for library callers.

## Python library

```python
from gpufree import GPUFeeClient

client = GPUFeeClient()
status = client.get_status()
balance = client.get_balance_overview()
instances = client.list_instances()
```

Run tests with `python3 -m unittest discover -s tests -v`.
