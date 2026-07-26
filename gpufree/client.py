"""
GPUFee.cn API client — authentication, balance, instances, products.

All amounts returned by the API are in **分** (1/100 yuan).
Use ``_yuan()`` to format them as human-readable yuan strings.
"""

import base64
import concurrent.futures
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_URL = "https://www.gpufree.cn"
REFRESH_ENDPOINT = f"{BASE_URL}/api/v1/user/token/refresh"

# Hardcoded fallback — extracted from the SPA's OAuth client registration.
# The client automatically prefers the client_id found inside stored JWTs.
_DEFAULT_CLIENT_ID = "2nT1FAzUbAaUVVmmtM9t8t3kfKq"

_PACKAGE_DIR = Path(__file__).resolve().parent
_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
DEFAULT_TOKEN_FILE = _CONFIG_HOME / "gpufree" / "token-state.json"
_LEGACY_TOKEN_FILE = _PACKAGE_DIR.parent / "token-state.json"
TOKEN_REFRESH_LEEWAY = 300


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _b64url_decode(payload: str) -> dict:
    """Decode a base64url-encoded JWT payload (no signature verification)."""
    padded = payload + "=" * (-len(payload) % 4)
    raw = base64.urlsafe_b64decode(padded)
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError("JWT payload must be a JSON object")
    return decoded


def _extract_client_id(state: dict) -> str | None:
    """Try to extract client_id from stored JWT tokens.

    Checks (in order):
      1. id_token  → client_id claim
      2. id_token  → azp claim (authorized party)
      3. id_token  → aud claim (first element if list)
      4. access_token → client_id claim
    """
    for token_key in ("id_token", "access_token"):
        token = state.get(token_key)
        if not isinstance(token, str) or not token:
            continue
        try:
            parts = token.split(".")
            if len(parts) < 2:
                continue
            claims = _b64url_decode(parts[1])
        except Exception:
            continue

        for claim in ("client_id", "azp"):
            val = claims.get(claim)
            if isinstance(val, str) and val:
                return val

        aud = claims.get("aud")
        if isinstance(aud, list) and aud:
            return str(aud[0])
        if isinstance(aud, str) and aud:
            return aud

    return None


def _yuan(cents: int | None) -> str:
    """Convert balance cents to yuan string (e.g. 16571 → '165.71').

    No thousands separators — the output is meant to be machine-readable
    (e.g. piped into ``jq``/``bc`` from cron scripts).
    """
    if cents is None:
        return "?"
    sign = "-" if cents < 0 else ""
    yuan, remainder = divmod(abs(cents), 100)
    return f"{sign}{yuan}.{remainder:02d}"


def _format_duration(seconds: int) -> str:
    """Format a non-negative duration as days, hours, and minutes."""
    days, remainder = divmod(max(seconds, 0), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    return f"{days}d {hours}h {minutes}m"


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write private JSON data with restrictive permissions."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(
        prefix=".gpufree-token-", dir=path.parent, text=True
    )
    tmppath = Path(tmpname)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            json.dump(data, fh, ensure_ascii=True, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmppath, path)
    finally:
        if fd >= 0:
            os.close(fd)
        tmppath.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# API Client
# ---------------------------------------------------------------------------
class GPUFeeClient:
    """Authenticated client for gpufree.cn APIs.

    Usage::

        from gpufree import GPUFeeClient

        client = GPUFeeClient()                     # uses default token file
        client = GPUFeeClient(token_file=Path("./my-tokens.json"))
        client = GPUFeeClient(client_id="overridden-client-id")

        status = client.get_status()
        print(status["balance_summary"]["balance_yuan"])
    """

    def __init__(
        self,
        token_file: str | Path | None = None,
        client_id: str | None = None,
    ):
        use_default_token_file = (
            token_file is None or Path(token_file) == DEFAULT_TOKEN_FILE
        )
        self.token_file = (
            Path(token_file) if token_file is not None else DEFAULT_TOKEN_FILE
        )
        self._state: dict = {}
        self._client_id: str | None = client_id
        self._legacy_token_file = (
            _LEGACY_TOKEN_FILE if use_default_token_file else None
        )
        self._refresh_lock = threading.Lock()
        self._load_state()

    # ---- client_id resolution ----------------------------------------------
    @property
    def client_id(self) -> str:
        """Resolve client_id: explicit arg > JWT extraction > hardcoded default."""
        return self._resolve_client_id()[0]

    @property
    def client_id_source(self) -> str:
        """Describe where the active client_id came from."""
        return self._resolve_client_id()[1]

    def _resolve_client_id(self) -> tuple[str, str]:
        if self._client_id:
            return self._client_id, "explicit argument"
        extracted = _extract_client_id(self._state)
        if extracted:
            return extracted, "stored JWT"
        return _DEFAULT_CLIENT_ID, "built-in default"

    # ---- state persistence ------------------------------------------------
    def _load_state(self) -> None:
        source = self.token_file
        should_migrate = (
            not source.exists()
            and self._legacy_token_file is not None
            and self._legacy_token_file.exists()
        )
        if should_migrate:
            source = self._legacy_token_file

        try:
            with source.open(encoding="utf-8") as fh:
                self._state = json.load(fh)
        except FileNotFoundError:
            self._state = {}
            return
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                f"Cannot read token state from {source}: {exc}"
            ) from exc

        if not isinstance(self._state, dict):
            raise RuntimeError(
                f"Invalid token state in {source}. Run: gpufree login"
            )

        if should_migrate:
            self._save_state()

    def _save_state(self) -> None:
        try:
            _atomic_write_json(self.token_file, self._state)
        except OSError as exc:
            raise RuntimeError(
                f"Cannot save token state to {self.token_file}: {exc}"
            ) from exc

    # ---- token introspection -----------------------------------------------
    def _token_expiry(self, token_name: str = "access_token") -> tuple[int, int] | None:
        """Return (exp, iat) timestamps from a stored JWT, or None."""
        token = self._state.get(token_name)
        if not token:
            return None
        try:
            claims = _b64url_decode(token.split(".")[1])
            exp = claims.get("exp")
            iat = claims.get("iat", 0)
            if not isinstance(exp, int) or not isinstance(iat, int):
                return None
            return exp, iat
        except Exception:
            return None

    def check_expiry(self) -> dict:
        """Report access and ID token expiry without contacting the server."""
        now = int(time.time())
        tokens = {}
        for token_name in ("access_token", "id_token"):
            exp_info = self._token_expiry(token_name)
            if not exp_info:
                tokens[token_name] = {"status": "missing_or_invalid"}
                continue
            exp_ts, iat_ts = exp_info
            remaining = exp_ts - now
            tokens[token_name] = {
                "status": "valid" if remaining > 0 else "expired",
                "issued_at": iat_ts,
                "expires_at": exp_ts,
                "lifetime_seconds": exp_ts - iat_ts,
                "remaining_seconds": remaining,
            }

        usable_expiries = [
            info["remaining_seconds"]
            for info in tokens.values()
            if "remaining_seconds" in info
        ]
        session_remaining = min(usable_expiries) if usable_expiries else None
        if session_remaining is None:
            status = "unknown"
            message = "No valid JWT expiry information in token state"
        elif session_remaining <= 0:
            status = "refresh_required"
            message = "A session token has expired; refresh before the next API call"
        elif session_remaining <= TOKEN_REFRESH_LEEWAY:
            status = "refresh_due"
            message = (
                "A session token is close to expiry and will be refreshed "
                "automatically"
            )
        else:
            status = "valid"
            message = f"Session token valid for {_format_duration(session_remaining)}"
        return {
            "status": status,
            "message": message,
            "now": now,
            "refresh_leeway_seconds": TOKEN_REFRESH_LEEWAY,
            "tokens": tokens,
        }

    def _refresh_due(self) -> bool:
        """Return whether a stored session JWT is close to expiry."""
        return self.check_expiry()["status"] in {
            "refresh_due",
            "refresh_required",
        }

    # ---- token management -------------------------------------------------
    @property
    def access_token(self) -> str | None:
        return self._state.get("access_token")

    @property
    def refresh_token_value(self) -> str | None:
        return self._state.get("refresh_token")

    def refresh_token(self) -> dict:
        """Refresh browser-style auth cookies and persist rotated tokens."""
        with self._refresh_lock:
            return self._refresh_token_unlocked()

    def _refresh_token_unlocked(self) -> dict:
        """Refresh tokens while the caller holds ``_refresh_lock``."""
        access_token = self._state.get("access_token")
        refresh_token = self._state.get("refresh_token")
        id_token_expiry = self._token_expiry("id_token")
        stale_id_token = bool(
            id_token_expiry
            and id_token_expiry[0] <= int(time.time()) + TOKEN_REFRESH_LEEWAY
        )
        if not refresh_token:
            raise RuntimeError(
                "Token state does not contain a refresh_token. Run: gpufree login"
            )

        cookie_values = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": self._state.get("id_token"),
        }
        cookie_header = "; ".join(
            f"{name}={value}"
            for name, value in cookie_values.items()
            if value
        )

        req = urllib.request.Request(
            REFRESH_ENDPOINT,
            data=b"",
            headers={
                "Accept": "application/json",
                "Cookie": cookie_header,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                set_cookie_headers = resp.headers.get_all("Set-Cookie", [])
        except urllib.error.HTTPError as exc:
            detail = exc.reason
            try:
                err_body = json.load(exc)
                detail = (
                    err_body.get("msg")
                    or err_body.get("error_description")
                    or err_body.get("error")
                    or detail
                )
            except Exception:
                pass
            hint = " Run: gpufree login" if exc.code in (400, 401) else ""
            raise RuntimeError(
                f"Token refresh failed (HTTP {exc.code}): {detail}.{hint}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Token refresh endpoint unreachable: {exc.reason}"
            ) from exc

        try:
            payload = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                "Token refresh endpoint returned invalid JSON"
            ) from exc

        if not isinstance(payload, dict):
            raise RuntimeError("Token refresh endpoint returned an invalid response")

        if str(payload.get("code")) != "200":
            detail = payload.get("msg") or payload.get("error") or "unknown error"
            raise RuntimeError(
                f"Token refresh failed: {detail}. Run: gpufree login"
            )

        refreshed_tokens: dict[str, str] = {}
        try:
            for header in set_cookie_headers:
                cookies = SimpleCookie()
                cookies.load(header)
                for name in ("access_token", "refresh_token", "id_token"):
                    if name in cookies and cookies[name].value:
                        refreshed_tokens[name] = cookies[name].value
        except CookieError as exc:
            raise RuntimeError(
                "Token refresh endpoint returned invalid Set-Cookie headers"
            ) from exc

        if not refreshed_tokens.get("access_token"):
            raise RuntimeError(
                "Token refresh succeeded but returned no new access_token. "
                "Run: gpufree login"
            )

        # Avoid refreshing on every request when the endpoint omits a stale ID token.
        if stale_id_token and "id_token" not in refreshed_tokens:
            self._state.pop("id_token", None)
        self._state.update(refreshed_tokens)
        self._save_state()
        return self._state.copy()

    def _ensure_access_token(self) -> str:
        """Return a usable token, refreshing once when missing or close to expiry."""
        if self.access_token and not self._refresh_due():
            return self.access_token

        with self._refresh_lock:
            if not self.access_token or self._refresh_due():
                self._refresh_token_unlocked()

        access_token = self.access_token
        if not access_token:
            raise RuntimeError(
                "Token refresh succeeded but no access_token is available. "
                "Run: gpufree login"
            )
        return access_token

    def _refresh_after_auth_failure(self, rejected_token: str) -> None:
        """Refresh unless another thread already replaced the rejected token."""
        with self._refresh_lock:
            if self.access_token == rejected_token:
                self._refresh_token_unlocked()

    # ---- HTTP helpers -----------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        retry_auth: bool = True,
        retry_network: bool = True,
        timeout: int = 30,
    ) -> dict:
        """Make an authenticated API request with one auth-refresh retry."""
        access_token = self._ensure_access_token() if retry_auth else self.access_token
        if not access_token:
            raise RuntimeError(
                "Token state does not contain an access_token. Run: gpufree login"
            )

        url = f"{BASE_URL}{path}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        }
        data = None
        if body is not None:
            data = urllib.parse.urlencode(body).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and retry_auth:
                self._refresh_after_auth_failure(access_token)
                return self._request(
                    method,
                    path,
                    body,
                    retry_auth=False,
                    retry_network=retry_network,
                    timeout=timeout,
                )
            if exc.code == 401:
                raise RuntimeError(
                    "API authentication failed after token refresh. "
                    "Run: gpufree login"
                ) from exc
            detail = ""
            try:
                detail = json.load(exc).get("message", "")
            except Exception:
                pass
            raise RuntimeError(
                f"API {method} {path} failed (HTTP {exc.code}): {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            is_timeout = (
                isinstance(exc.reason, TimeoutError)
                or "timed out" in str(exc.reason).lower()
            )
            if method == "GET" and retry_network and is_timeout:
                time.sleep(1)
                return self._request(
                    method,
                    path,
                    body,
                    retry_auth=retry_auth,
                    retry_network=False,
                    timeout=timeout,
                )
            raise RuntimeError(f"API {path} unreachable: {exc.reason}") from exc

        try:
            payload = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                f"API {method} {path} returned invalid JSON"
            ) from exc

        if isinstance(payload, dict) and str(payload.get("code")) == "43003":
            if retry_auth:
                self._refresh_after_auth_failure(access_token)
                return self._request(
                    method,
                    path,
                    body,
                    retry_auth=False,
                    retry_network=retry_network,
                    timeout=timeout,
                )
            raise RuntimeError(
                "API authentication failed after token refresh. "
                "Run: gpufree login"
            )

        return payload

    def _get(self, path: str, *, timeout: int = 30) -> dict:
        return self._request("GET", path, timeout=timeout)

    # =====================================================================
    # User & Auth
    # =====================================================================

    def get_userinfo(self) -> dict:
        """Get current user profile information."""
        return self._get("/api/v1/oidc/userinfo")

    def get_unread_count(self) -> dict:
        """Get unread message count."""
        return self._get("/api/v1/user/message/unread/count")

    def get_messages(self) -> dict:
        """Get message list."""
        return self._get("/api/v1/user/message/list")

    def get_invitation_code(self) -> dict:
        """Get user invitation code."""
        return self._get("/api/v1/user/invitation/code")

    # =====================================================================
    # Balance & Billing  (amounts in 分)
    # =====================================================================

    def get_balance(self) -> dict:
        """Get raw account balance (amounts in 分)."""
        return self._get("/api/v1/billing/public/balance")

    def get_balance_overview(self) -> dict:
        """Get balance overview (amounts in 分)."""
        return self._get("/api/v1/billing/public/balance/overview")

    def get_balance_history(self) -> dict:
        """Get balance change history."""
        return self._get("/api/v1/billing/public/balance/history")

    def get_orders(self) -> dict:
        """Get billing order records."""
        return self._get("/api/v1/billing/public/order/records")

    def get_transactions(self) -> dict:
        """Get transaction / bill records."""
        return self._get("/api/v1/billing/public/bill/transaction/records")

    def get_daily_bills(self) -> dict:
        """Get daily bill records."""
        return self._get("/api/v1/billing/public/bill/daily-bill/records")

    def get_recharge_state(self) -> dict:
        """Get recharge order state."""
        return self._get("/api/v1/billing/public/recharge/state")

    def get_vouchers(self) -> dict:
        """Get available vouchers / coupons."""
        return self._get("/api/v1/user/vouchers")

    # =====================================================================
    # Instances (Jupyter / container)
    # =====================================================================

    def list_instances(self) -> dict:
        """List all Jupyter/container instances."""
        query = urllib.parse.urlencode({"page_no": 1, "page_size": 200})
        return self._get(
            f"/api/v1/jupyter/list_instance_pages?{query}", timeout=15
        )

    def get_instance_status_tags(self) -> dict:
        """Get status tags for instances."""
        return self._get("/api/v1/jupyter/status_tag")

    def get_instance_ide_res(self) -> dict:
        """Get IDE resource info for instances."""
        return self._get("/api/v1/jupyter/ide_res")

    def get_instance_left_copy(self) -> dict:
        """Get remaining copy count."""
        return self._get("/api/v1/jupyter/left_copy")

    # =====================================================================
    # Dashboard Resources
    # =====================================================================

    def get_dashboard_instances(self) -> dict:
        """Get dashboard resource instance overview."""
        return self._get("/api/v1/dashboard/res/instance")

    def get_dashboard_storage(self) -> dict:
        """Get dashboard storage overview."""
        return self._get("/api/v1/dashboard/res/storage")

    # =====================================================================
    # Products
    # =====================================================================

    def get_hot_products(self) -> dict:
        """Get featured / hot products."""
        return self._get("/api/v1/products/hot")

    def list_products(self) -> dict:
        """List all available products."""
        return self._get("/api/v1/products/list")

    def list_gpu_names(self) -> dict:
        """List all available GPU model names."""
        return self._get("/api/v1/products/listGpuNames")

    def get_product_tags(self) -> dict:
        """Get product category tags."""
        return self._get("/api/v1/products/tags")

    def get_sku_price(self) -> dict:
        """Get product SKU pricing."""
        return self._get("/api/v1/billing/public/product/sku-price/list")

    # =====================================================================
    # SSH Keys
    # =====================================================================

    def list_ssh_keys(self) -> dict:
        """List registered SSH public keys."""
        return self._get("/api/v1/pub_key/list")

    # =====================================================================
    # File Storage
    # =====================================================================

    def list_file_storage(self) -> dict:
        """List file storage entries."""
        return self._get("/api/v1/file_storage/list")

    # =====================================================================
    # Images
    # =====================================================================

    def list_public_images(self) -> dict:
        """List public container images."""
        return self._get("/api/v1/images/public/display_list")

    # =====================================================================
    # Composite queries
    # =====================================================================

    def get_status(self) -> dict:
        """Composite: collect balance, instances, userinfo, dashboard in one call.

        Returns raw data plus ``balance_summary`` (yuan-formatted) and
        ``instances_summary`` (key fields per instance).
        """
        calls = [
            ("user", self.get_userinfo),
            ("balance", self.get_balance_overview),
            ("instances", self.list_instances),
            ("dashboard", self.get_dashboard_instances),
            ("unread_messages", self.get_unread_count),
        ]
        result: dict = {}

        try:
            self._ensure_access_token()
        except RuntimeError as exc:
            result.update({label: {"error": str(exc)} for label, _ in calls})
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(calls), thread_name_prefix="gpufree-status"
            ) as executor:
                futures = {
                    label: executor.submit(method) for label, method in calls
                }
                for label, _ in calls:
                    try:
                        result[label] = futures[label].result()
                    except RuntimeError as exc:
                        result[label] = {"error": str(exc)}

        # Human-readable balance summary
        bal = result.get("balance", {})
        if isinstance(bal, dict):
            data = bal.get("data", {})
            if isinstance(data, dict):
                result["balance_summary"] = {
                    "balance_yuan": _yuan(data.get("balance")),
                    "amount_yuan": _yuan(data.get("amount")),
                    "cost_yuan": _yuan(data.get("cost")),
                }

        # Instance summary
        inst = result.get("instances", {})
        if isinstance(inst, dict):
            inst_data = inst.get("data", {})
            if isinstance(inst_data, dict):
                items = inst_data.get("dataList") or []
                instances_summary = []
                for item in items if isinstance(items, list) else []:
                    if not isinstance(item, dict):
                        continue
                    disk_info = item.get("local_disk_info") or {}
                    if not isinstance(disk_info, dict):
                        disk_info = {}
                    instances_summary.append(
                        {
                            "name": item.get("nick_name", "?"),
                            "gpu": item.get("product_name", "?"),
                            "count": item.get("req_gpu_amount", 0),
                            "dc": item.get("data_center_name", "?"),
                            "jupyter": (
                                "running"
                                if item.get("jupyter_status") == 1
                                else "stopped"
                            ),
                            "disk_root": disk_info.get("root_fs_used_rate", "?"),
                            "ssh": item.get("ssh_command", ""),
                        }
                    )
                result["instances_summary"] = instances_summary

        return result

    # =====================================================================
    # Interactive login
    # =====================================================================

    @staticmethod
    def login_interactive(token_file: str | Path) -> int:
        """Guide the user to extract tokens from their browser and save them."""
        js_snippet = (
            "copy(JSON.stringify({"
            "access_token:document.cookie.match(/access_token=([^;]+)/)?.[1],"
            "refresh_token:document.cookie.match(/refresh_token=([^;]+)/)?.[1],"
            "id_token:document.cookie.match(/id_token=([^;]+)/)?.[1]"
            "}))"
        )
        print(
            "╔══════════════════════════════════════════════════════════════╗\n"
            "║              GPUFee.cn  Interactive Login                    ║\n"
            "╠══════════════════════════════════════════════════════════════╣\n"
            "║  1. Open https://www.gpufree.cn/login in a private window  ║\n"
            "║  2. Log in with your phone number + password                ║\n"
            "║  3. Solve the slide captcha                                 ║\n"
            "║  4. Open DevTools (F12) → Console                           ║\n"
            "║  5. Paste the ONE-LINE script shown below and press Enter   ║\n"
            "║  6. Paste the copied JSON below, then close that window.    ║\n"
            "╚══════════════════════════════════════════════════════════════╝"
        )
        print()
        print(
            "  \033[1;33m┌─ Copy the line below into the browser Console "
            "────────────┐\033[0m"
        )
        print(f"  \033[1;33m│\033[0m \033[1;37m{js_snippet}\033[0m")
        print(
            "  \033[1;33m└──────────────────────────────────────────────────────────"
            "┘\033[0m"
        )
        print()
        try:
            raw = input("Paste token JSON here: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nLogin cancelled.", file=sys.stderr)
            return 1

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"Error: invalid JSON: {exc}", file=sys.stderr)
            return 1

        if not isinstance(data, dict):
            print("Error: token JSON must be an object", file=sys.stderr)
            return 1

        required = ["access_token", "refresh_token"]
        missing = [k for k in required if not data.get(k)]
        if missing:
            print(f"Error: missing required fields: {missing}", file=sys.stderr)
            return 1

        state = {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "id_token": data.get("id_token", ""),
            "token_type": data.get("token_type", "Bearer"),
            "expires_in": data.get("expires_in", 604800),
            "scope": data.get(
                "scope", "openid profile offline_access email phone user_info"
            ),
        }

        try:
            _atomic_write_json(Path(token_file), state)
        except OSError as exc:
            print(f"Error: cannot save token state: {exc}", file=sys.stderr)
            return 1

        # Verify
        client = GPUFeeClient(token_file=token_file)
        try:
            user = client.get_userinfo()
            phone = user.get("phone_number", "unknown")
            print(f"✓ Login successful! Phone: {phone}")
            print(f"  Tokens saved to: {token_file}")
        except RuntimeError as exc:
            print(f"⚠ Tokens saved but verification failed: {exc}", file=sys.stderr)
            return 1

        return 0
