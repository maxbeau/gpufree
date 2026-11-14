"""Stable machine-readable CLI output helpers."""

from __future__ import annotations

import json
import http.client
import re
import urllib.error
from time import perf_counter
from datetime import datetime, timezone

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 10
EXIT_NO_STOCK = 11
EXIT_BALANCE = 12
EXIT_CONFLICT = 13
EXIT_NETWORK = 14
EXIT_VALIDATION = 15
EXIT_LOCKED = 16
EXIT_DRIFT = 17


def redact(value):
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if any(part in str(key).casefold()
                                          for part in ("password", "token", "cookie", "secret"))
                      else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


def error_details(exc):
    text = str(exc)
    code = getattr(exc, "code", None)
    code_text = str(code) if code is not None else None
    explicit_kind = getattr(exc, "kind", None)
    if explicit_kind in {"verify_network_unreachable", "verify_failed"}:
        return 15, {"kind": explicit_kind, "platform_code": code_text, "retryable": explicit_kind == "verify_network_unreachable",
                    "action": "use_via_or_check_network" if explicit_kind == "verify_network_unreachable" else "inspect_instance_probe",
                    "message": text}
    if code_text == "53523":
        return EXIT_NO_STOCK, {"kind": "no_stock", "platform_code": code_text, "retryable": True, "action": "wait_and_retry", "message": text}
    if code_text == "53524":
        return EXIT_CONFLICT, {"kind": "state_conflict", "platform_code": code_text, "retryable": False, "action": "refresh_state", "message": text}
    if code_text == "42408":
        return EXIT_BALANCE, {"kind": "insufficient_balance", "platform_code": code_text, "retryable": False, "action": "recharge_or_reduce_cost", "message": text}
    low = text.casefold()
    cause = getattr(exc, "__cause__", None)
    network_types = (ConnectionError, TimeoutError, http.client.HTTPException, urllib.error.URLError)
    if isinstance(exc, network_types) or isinstance(cause, network_types):
        return EXIT_NETWORK, {"kind": "network_error", "platform_code": code_text, "retryable": True,
                              "action": "retry_when_network_recovers", "message": text}
    if "gpu stock" in low and "timed out" in low:
        return EXIT_NO_STOCK, {"kind": "no_stock", "platform_code": code_text, "retryable": True, "action": "wait_and_retry", "message": text}
    if "unreachable" in low or "network" in low:
        return EXIT_NETWORK, {"kind": "network_error", "platform_code": code_text, "retryable": True, "action": "retry_when_network_recovers", "message": text}
    if "refresh" in low or "authentication" in low or "access_token" in low or "login" in low:
        return EXIT_AUTH, {"kind": "auth_required", "platform_code": code_text, "retryable": False, "action": "run_auth_import", "message": text}
    if isinstance(exc, (ValueError, TypeError)):
        return EXIT_VALIDATION, {"kind": "validation_error", "platform_code": code_text, "retryable": False, "action": "correct_input", "message": text}
    return 1, {"kind": "error", "platform_code": code_text, "retryable": False, "action": "inspect_message", "message": text}


def make_envelope(command, data=None, *, error=None, warnings=None, started=None, cli_version="0.2.0", stale=None):
    meta = {"checked_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "duration_ms": max(0, int((perf_counter() - started) * 1000)) if started is not None else 0,
            "cli_version": cli_version}
    if stale:
        meta.update(stale)
    return {"ok": error is None, "schema": 1, "command": command,
            "data": redact(data), "warnings": warnings or [],
            "error": redact(error), "meta": meta}


def project(data, *, jq=None, template=None, quiet=False):
    if jq:
        node = data
        for segment in jq.split("."):
            if isinstance(node, list) and segment.isdigit():
                node = node[int(segment)]
            elif isinstance(node, dict) and segment in node:
                node = node[segment]
            else:
                raise ValueError(f"--jq path not found: {jq}")
        return node
    if template is not None:
        return re.sub(r"\{([^{}]+)\}", lambda m: str(_path(data, m.group(1))), template)
    if quiet:
        if isinstance(data, dict) and "instances" in data:
            return "\n".join(str(item.get("uuid", "")) for item in data["instances"])
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(data, ensure_ascii=False, indent=2)


def _path(data, path):
    node = data
    for segment in path.split("."):
        node = node[int(segment)] if isinstance(node, list) and segment.isdigit() else node[segment]
    return node
