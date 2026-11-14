"""Snapshot normalization and warning calculations."""

from __future__ import annotations

from datetime import datetime, timezone


def _timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _number(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.rstrip("%"))
        except ValueError:
            return None
    return None


def normalize(status, auth, stock_products, *, warn_balance_below=None, now=None):
    now = now or datetime.now(timezone.utc)
    balance_obj = status.get("balance", {})
    balance_data = balance_obj.get("data", {}) if isinstance(balance_obj, dict) else {}
    cents = balance_data.get("balance") if isinstance(balance_data, dict) else None
    balance_yuan = float(cents) / 100 if isinstance(cents, (int, float)) else None
    raw_instances = (status.get("instances", {}).get("data", {}).get("dataList", [])
                     if isinstance(status.get("instances"), dict) else [])
    products = stock_products if isinstance(stock_products, list) else []
    instances, burn = [], 0.0
    warnings = []
    for item in raw_instances if isinstance(raw_instances, list) else []:
        if not isinstance(item, dict):
            continue
        state_raw = item.get("status")
        state = {"3": "running", "5": "stopped", "1": "queued"}.get(str(state_raw), state_raw)
        name = item.get("product_name")
        product = next((p for p in products if p.get("product_name") == name), None)
        price = _number(product.get("current_price")) / 100 if product and _number(product.get("current_price")) is not None else None
        if state == "running" and price is not None:
            burn += price * (_number(item.get("req_gpu_amount") or item.get("req_amount")) or 1)
        disk = item.get("local_disk_info") if isinstance(item.get("local_disk_info"), dict) else {}
        disk_rate = _number(disk.get("root_fs_used_rate"))
        release = item.get("release_time") or item.get("release_at")
        release_dt = _timestamp(release)
        release_seconds = int((release_dt - now).total_seconds()) if release_dt else None
        uuid = item.get("instance_uuid") or item.get("webide_instance_uuid") or item.get("uuid")
        command = str(item.get("ssh_command") or "")
        endpoint = {"host": None, "port": None, "user": None}
        import shlex
        tokens = shlex.split(command)
        port = 22
        if "-p" in tokens:
            try:
                port = int(tokens[tokens.index("-p") + 1])
            except (ValueError, IndexError):
                port = None
        candidates = [token for token in tokens[1:] if not token.startswith("-") and
                      not (tokens.index(token) > 0 and tokens[tokens.index(token) - 1] in {"-p", "-i"})]
        if candidates:
            endpoint.update({"port": port, "user": candidates[-1].split("@")[0] if "@" in candidates[-1] else None,
                             "host": candidates[-1].split("@")[-1]})
        instances.append({"id": item.get("instance_id") or item.get("webide_instance_id") or item.get("id"),
                          "uuid": uuid, "name": item.get("nick_name") or item.get("webide_instance_name"),
                          "product": name, "data_center": item.get("data_center_name"),
                          "state": state, "start_mode": item.get("start_mode"),
                          "ssh": endpoint, "root_disk_used_percent": disk_rate,
                          "release_at": release, "release_in_seconds": release_seconds,
                          "price_yuan_per_hour": price,
                          "stock": _stock_for(products, name, item.get("data_center_id"))})
        if disk_rate is not None and disk_rate > 85:
            warnings.append({"kind": "disk_high", "severity": "warn", "detail": f"{uuid}: root disk {disk_rate:g}%"})
        if state == "stopped" and release_seconds is not None and release_seconds < 48 * 3600:
            warnings.append({"kind": "release_soon", "severity": "warn", "detail": str(uuid)})
    burn = round(burn, 6) if burn else None
    runway = balance_yuan / burn if balance_yuan is not None and burn else None
    if balance_yuan is not None and burn and balance_yuan < burn * 24:
        warnings.append({"kind": "low_balance", "severity": "warn", "detail": "balance is below 24h estimated burn"})
    elif warn_balance_below is not None and balance_yuan is not None and balance_yuan < warn_balance_below:
        warnings.append({"kind": "low_balance", "severity": "warn", "detail": f"balance is below {warn_balance_below:g} yuan"})
    if auth.get("remaining_seconds") is not None and auth["remaining_seconds"] < 48 * 3600:
        warnings.append({"kind": "auth_expiring", "severity": "warn", "detail": "access token expires within 48h"})
    return {"auth": auth, "balance_yuan": balance_yuan, "burn_yuan_per_hour": burn,
            "runway_hours": round(runway, 2) if runway is not None else None,
            "instances": instances}, warnings


def _stock_for(products, name, dc):
    product = next((p for p in products if p.get("product_name") == name), None)
    if not product:
        return None
    skus = product.get("product_skus") or []
    matches = [sku.get("gpu_nodes_left") for sku in skus if str(sku.get("datacenter_id")) == str(dc)]
    try:
        return sum(int(value or 0) for value in matches) if matches else None
    except (TypeError, ValueError):
        return None
