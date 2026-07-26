"""
GPUFee.cn API client — authentication, balance, instances, products.

All amounts returned by the API are in **分** (1/100 yuan).
Use ``_yuan()`` to format them as human-readable yuan strings.
"""

import base64
import json
import os
import sys
import tempfile
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
DEFAULT_TOKEN_FILE = _PACKAGE_DIR / "token-state.json"
TOKEN_REFRESH_LEEWAY = 300


# ---------------------------------------------------------------------------
# JWT helpers (public so other modules can reuse them)
# ---------------------------------------------------------------------------
def _b64url_decode(payload: str) -> dict:
    """Decode a base64url-encoded JWT payload (no signature verification)."""
    padded = payload + "=" * (4 - len(payload) % 4)
    raw = base64.urlsafe_b64decode(padded)
    return json.loads(raw)


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
    """Convert balance cents to yuan string (e.g. 16571 → '165.71')."""
    if cents is None:
        return "?"
    return f"{cents / 100:,.2f}"


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
        token_file: Path | None = None,
        client_id: str | None = None,
    ):
        self.token_file = Path(token_file or DEFAULT_TOKEN_FILE)
        self._state: dict = {}
        self._client_id: str | None = client_id
        self._load_state()

    # ---- client_id resolution ----------------------------------------------
    @property
    def client_id(self) -> str:
        """Resolve client_id: explicit arg > JWT extraction > hardcoded default."""
        if self._client_id:
            return self._client_id
        extracted = _extract_client_id(self._state)
        if extracted:
            return extracted
        return _DEFAULT_CLIENT_ID

    # ---- state persistence ------------------------------------------------
    def _load_state(self) -> None:
        try:
            with self.token_file.open(encoding="utf-8") as fh:
                self._state = json.load(fh)
        except FileNotFoundError:
            self._state = {}
            return
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Cannot read token state from {self.token_file}: {exc}"
            ) from exc

        if not isinstance(self._state, dict):
            raise RuntimeError(
                f"Invalid token state in {self.token_file}. Run: gpufree login"
            )

    def _save_state(self) -> None:
        fd, tmpname = tempfile.mkstemp(
            prefix=".gpufree-token-", dir=self.token_file.parent, text=True
        )
        tmppath = Path(tmpname)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, ensure_ascii=True, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmppath, self.token_file)
        finally:
            tmppath.unlink(missing_ok=True)

    # ---- token introspection -----------------------------------------------
    def _token_expiry(self, token_name: str = "access_token") -> tuple[int, int] | None:
        """Return (exp, iat) timestamps from a stored JWT, or None."""
        token = self._state.get(token_name)
        if not token:
            return None
        try:
            claims = _b64url_decode(token.split(".")[1])
            return (claims.get("exp", 0), claims.get("iat", 0))
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
            message = "A session token is close to expiry and will be refreshed automatically"
        else:
            status = "valid"
            message = f"Session token valid for {session_remaining // 60}m"
        return {
            "status": status,
            "message": message,
            "now": now,
            "refresh_leeway_seconds": TOKEN_REFRESH_LEEWAY,
            "tokens": tokens,
        }

    def _refresh_due(self) -> bool:
        """Return whether a stored session JWT is close to expiry."""
        now = int(time.time())
        expiries = []
        for token_name in ("access_token", "id_token"):
            exp_info = self._token_expiry(token_name)
            if exp_info:
                expiries.append(exp_info[0])
        return bool(expiries) and min(expiries) <= now + TOKEN_REFRESH_LEEWAY

    # ---- token management -------------------------------------------------
    @property
    def access_token(self) -> str | None:
        return self._state.get("access_token")

    @property
    def refresh_token_value(self) -> str | None:
        return self._state.get("refresh_token")

    def refresh_token(self) -> dict:
        """Refresh browser-style auth cookies and persist rotated tokens."""
        access_token = self._state.get("access_token")
        refresh_token = self._state.get("refresh_token")
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
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Token refresh endpoint returned invalid JSON"
            ) from exc

        if not isinstance(payload, dict):
            raise RuntimeError("Token refresh endpoint returned an invalid response")

        if payload.get("code") != 200:
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

        self._state.update(refreshed_tokens)
        self._save_state()
        return self._state.copy()

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
        if retry_auth and self._refresh_due():
            self.refresh_token()

        access_token = self.access_token
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
                payload = json.loads(raw) if raw else {}

                if (
                    isinstance(payload, dict)
                    and str(payload.get("code")) == "43003"
                ):
                    if retry_auth:
                        self.refresh_token()
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
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and retry_auth:
                self.refresh_token()
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
                return self._request(
                    method,
                    path,
                    body,
                    retry_auth=retry_auth,
                    retry_network=False,
                    timeout=timeout,
                )
            raise RuntimeError(f"API {path} unreachable: {exc.reason}") from exc

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
        result: dict = {}
        for label, method in [
            ("user", self.get_userinfo),
            ("balance", self.get_balance_overview),
            ("instances", self.list_instances),
            ("dashboard", self.get_dashboard_instances),
            ("unread_messages", self.get_unread_count),
        ]:
            try:
                result[label] = method()
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
                items = inst_data.get("dataList", [])
                result["instances_summary"] = [
                    {
                        "name": i.get("nick_name", "?"),
                        "gpu": i.get("product_name", "?"),
                        "count": i.get("req_gpu_amount", 0),
                        "dc": i.get("data_center_name", "?"),
                        "jupyter": "running" if i.get("jupyter_status") == 1 else "stopped",
                        "disk_root": i.get("local_disk_info", {}).get("root_fs_used_rate", "?"),
                        "ssh": i.get("ssh_command", ""),
                    }
                    for i in items
                ]

        return result

    # =====================================================================
    # Interactive login
    # =====================================================================

    @staticmethod
    def login_interactive(token_file: Path) -> int:
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
        print("  \033[1;33m┌─ Copy the line below into the browser Console ────────────┐\033[0m")
        print(f"  \033[1;33m│\033[0m \033[1;37m{js_snippet}\033[0m")
        print("  \033[1;33m└──────────────────────────────────────────────────────────┘\033[0m")
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
            "scope": data.get("scope", "openid profile offline_access email phone user_info"),
        }

        fd, tmpname = tempfile.mkstemp(
            prefix=".gpufree-token-", dir=token_file.parent, text=True
        )
        tmppath = Path(tmpname)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=True, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmppath, token_file)
        finally:
            tmppath.unlink(missing_ok=True)

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
