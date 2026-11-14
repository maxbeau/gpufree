"""Token import parsing and local auth status classification."""

from __future__ import annotations

import json
import os
import tempfile
import time
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

from gpufree.client import _atomic_write_json, _b64url_decode

TOKEN_KEYS = ("access_token", "refresh_token", "id_token")
AUTH_ACTIONS = {
    "valid": "Continue using the CLI; avoid refreshing the same browser session.",
    "expiring": "Run gpufree auth keepalive, or import a fresh browser session.",
    "refresh_rejected_rotated": "Log in in the browser, then import its cookies with gpufree auth import.",
    "refresh_rejected_expired": "Log in in the browser, then import its cookies with gpufree auth import.",
    "missing": "Log in at gpufree.cn and run gpufree auth import --stdin.",
    "network_error": "Restore network access, then retry gpufree auth status --probe.",
}


def parse_token_input(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        state = parsed.get("data", parsed)
        if isinstance(state, dict) and isinstance(state.get("data"), dict):
            state = state["data"]
        if not isinstance(state, dict):
            raise ValueError("token JSON key 'data' must contain an object")
        tokens = {key: state.get(key) for key in TOKEN_KEYS}
    else:
        cookie_text = raw.strip()
        if cookie_text.casefold().startswith("cookie:"):
            cookie_text = cookie_text.split(":", 1)[1].strip()
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_text)
        except CookieError as exc:
            raise ValueError("cookie string is malformed near a token segment") from exc
        tokens = {key: cookies[key].value if key in cookies else None for key in TOKEN_KEYS}
    for key in TOKEN_KEYS:
        value = tokens.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"missing or invalid token key '{key}'")
        if key != "refresh_token":
            parts = value.split(".")
            if len(parts) != 3:
                raise ValueError(f"{key} is not a complete JWT (expected three segments)")
            try:
                claims = _b64url_decode(parts[1])
            except Exception as exc:
                raise ValueError(f"{key} payload is truncated or invalid JSON") from exc
            if key == "id_token" and "gpufree.cn" not in str(claims.get("iss", "")):
                raise ValueError("id_token key 'iss' is not gpufree.cn")
    return {key: tokens[key] for key in TOKEN_KEYS}


def classify(client, *, probe=False):
    if not client._state:
        category = "missing"
    else:
        expiry = client._token_expiry("access_token")
        if expiry is None:
            category = "missing"
        else:
            remaining = expiry[0] - int(time.time())
            category = "expiring" if remaining < 48 * 3600 else "valid"
    if probe and category not in {"missing"}:
        try:
            client.refresh_token()
            expiry = client._token_expiry("access_token")
            category = "valid" if expiry and expiry[0] - int(time.time()) >= 48 * 3600 else "expiring"
        except Exception as exc:
            message = str(exc).casefold()
            if "unreachable" in message or "network" in message:
                category = "network_error"
            elif "expired" in message or "invalid" in message or "401" in message or "400" in message:
                category = "refresh_rejected_expired"
            else:
                category = "refresh_rejected_rotated"
    return {"classification": category, "action": AUTH_ACTIONS[category],
            "remaining_seconds": (client._token_expiry("access_token")[0] - int(time.time())
                                  if client._token_expiry("access_token") else None),
            "probe": bool(probe)}


def import_tokens(client, state, *, verify=True):
    """Stage and verify a token set, then atomically replace the configured file."""
    target = Path(client.token_file)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".gpufree-import-", dir=target.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        _atomic_write_json(temp_path, state)
        if verify:
            from gpufree.client import GPUFeeClient
            staged = GPUFeeClient(token_file=temp_path, client_id=client._client_id)
            staged.refresh_token()
            state = staged._state
        backup = target.with_name(target.name + ".bak")
        moved_old = False
        if target.exists():
            os.replace(target, backup)
            moved_old = True
        try:
            _atomic_write_json(target, state)
        except Exception:
            if moved_old and backup.exists():
                os.replace(backup, target)
            raise
        return {"imported": True, "verified": verify, "backup_created": backup.exists()}
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        temp_path.unlink(missing_ok=True)
        temp_path.with_name(temp_path.name + ".lock").unlink(missing_ok=True)

