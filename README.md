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

### Available API methods

| Category | Methods |
| --- | --- |
| User | `get_userinfo()`, `get_unread_count()`, `get_messages()`, `get_invitation_code()` |
| Balance | `get_balance()`, `get_balance_overview()`, `get_balance_history()`, `get_orders()`, `get_transactions()`, `get_daily_bills()`, `get_recharge_state()`, `get_vouchers()` |
| Instances | `list_instances()`, `get_instance_status_tags()`, `get_instance_ide_res()`, `get_instance_left_copy()` |
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
balance formatting, composite status concurrency, and CLI routing.

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
│   └── test_client.py
├── .gitignore
├── pyproject.toml
└── README.md
```

## Known limitation

The service's slide captcha prevents fully automated login. `gpufree login`
therefore requires copying token JSON from a dedicated private browser window.
Close that window after copying the tokens so the browser does not rotate the
same refresh token independently.
