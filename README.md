# gpufree — 算力自由 API Client

A Python client + CLI for [gpufree.cn](https://www.gpufree.cn), a GPU cloud
platform. Handles authentication (cookie-based token refresh), account
balance queries, instance listing, and product browsing.

## Quick start

```bash
# Show everything
gpufree status

# Balance only
gpufree balance

# List your GPU instances
gpufree instances

# Check when the current token expires
gpufree check

# Refresh browser-style auth tokens now
gpufree refresh

# Login (when the refresh token is no longer usable)
gpufree login
```

## Installation

The package lives at `~/gpufree/` and is already wired up:

```bash
~/gpufree/gpufree                    # executable wrapper
~/.local/bin/gpufree                 # symlink → ~/gpufree/gpufree
```

Add to `$PYTHONPATH` (or keep the ``sys.path`` hack in the wrapper) if you
want to import the module from other scripts.

## Use as a Python library

```python
from gpufree import GPUFeeClient

client = GPUFeeClient()                     # uses ~/gpufree/token-state.json
client = GPUFeeClient(token_file="...")     # custom token path
client = GPUFeeClient(client_id="...")      # override reported client_id

# One-shot status (balance + instances + user)
status = client.get_status()
print(status["balance_summary"]["balance_yuan"])   # "165.71"

# Individual endpoints
user   = client.get_userinfo()
bal    = client.get_balance_overview()
inst   = client.list_instances()
prods  = client.get_hot_products()
```

### Available API methods

| Category   | Methods |
|------------|---------|
| **User**   | `get_userinfo()`, `get_unread_count()`, `get_messages()`, `get_invitation_code()` |
| **Balance**| `get_balance()`, `get_balance_overview()`, `get_balance_history()`, `get_orders()`, `get_transactions()`, `get_daily_bills()`, `get_recharge_state()`, `get_vouchers()` |
| **Instances** | `list_instances()`, `get_instance_status_tags()`, `get_instance_ide_res()`, `get_instance_left_copy()` |
| **Dashboard** | `get_dashboard_instances()`, `get_dashboard_storage()` |
| **Products** | `list_products()`, `get_hot_products()`, `list_gpu_names()`, `get_product_tags()`, `get_sku_price()` |
| **Other**  | `list_ssh_keys()`, `list_file_storage()`, `list_public_images()` |
| **Composite** | `get_status()` — returns `balance_summary` and `instances_summary` alongside raw data |
| **Auth**   | `refresh_token()`, `check_expiry()`, `login_interactive()` |

## Token management

Tokens are stored in `~/gpufree/token-state.json` (0600 permissions). The
file contains:

```json
{
  "access_token":  "<JWT — valid ~7 days>",
  "refresh_token": "<browser cookie — used to refresh authentication>",
  "id_token":      "<JWT — contains client_id for auto-extraction>",
  "token_type":    "Bearer",
  "expires_in":    604800,
  "scope":         "openid profile offline_access email phone user_info"
}
```

- `gpufree check` — see how long the current access_token is valid
- `gpufree refresh` — refresh tokens immediately through the cookie endpoint
- `gpufree login` — re-login when the refresh token is unavailable or invalid
- `--token-file PATH` — use a custom token file location
- `--client-id ID` — override the client_id reported by `gpufree client-id`

When a protected API reports an expired login, the client attempts one token
refresh and retries the original request once. If the refresh token is also
invalid, run `gpufree login`.

### How client_id reporting works

The OAuth `client_id` is resolved in this order:

1. `--client-id` CLI argument (highest priority)
2. Extracted from the stored `id_token` or `access_token` JWT
3. Hardcoded default (`2nT1FAzUbAaUVVmmtM9t8t3kfKq`)

Run `gpufree client-id` to see which source is active.

Cookie-based token refresh does not use `client_id` or `client_secret`.

## Automation / cron

```bash
# Add to crontab to check balance every hour
0 * * * * /home/maxbeau/gpufree/gpufree status >> ~/gpufree-status.log 2>&1

# Alert when balance drops below ¥100 (using jq)
*/30 * * * * BALANCE=$(/home/maxbeau/gpufree/gpufree status 2>/dev/null | jq -r '.balance_summary.balance_yuan') && [ "$(echo "$BALANCE < 100" | bc)" = 1 ] && echo "Low balance: ¥$BALANCE" | mail -s "GPUFee Alert" you@example.com
```

## Files

```
~/gpufree/
├── README.md           # This file
├── gpufree             # Executable wrapper
├── __init__.py          # Package init — exports GPUFeeClient
├── client.py            # GPUFeeClient class + auth helpers
├── cli.py               # CLI argument parsing
└── token-state.json     # Authentication tokens (0600, gitignored)
```

## Known limitations

- **Slide captcha** prevents fully automated login.  `gpufree login`
  requires pasting tokens from the browser.
- Automatic refresh is request-driven rather than scheduled. It runs only
  when an API reports that authentication has expired.
