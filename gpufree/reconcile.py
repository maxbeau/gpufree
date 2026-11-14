"""Mockable instance ensure state machine."""

from __future__ import annotations

import random
import time

from gpufree.client import PlatformError


class EnsureTimeout(RuntimeError):
    code = 53523


class EnsureConflict(RuntimeError):
    code = 53524


class VerificationError(RuntimeError):
    def __init__(self, message, kind="verify_failed"):
        super().__init__(message)
        self.kind = kind


def ensure(client, selector, desired, *, mode="gpu", deadline=2700, verify=(),
           allow_restart=False, plan_only=False, progress=None, sleep=time.sleep,
           clock=time.monotonic, jitter=lambda: random.uniform(-3, 3), verifier=None):
    if desired not in {"running", "stopped"}:
        raise ValueError("desired state must be running or stopped")
    item = client.resolve_instance(selector)
    current = str(item.get("status"))
    current_mode = str(item.get("start_mode", "gpu"))
    steps = []
    if desired == "running" and current == "5":
        steps.append({"action": "start", "mode": mode})
    elif desired == "running" and current == "3" and current_mode != mode:
        if not allow_restart:
            raise EnsureConflict("running with GPU conflicts with current nogpu mode; pass --allow-restart")
        steps.extend([{"action": "stop"}, {"action": "start", "mode": mode}])
    elif desired == "stopped" and current != "5":
        steps.append({"action": "stop"})
    if plan_only:
        return {"plan": steps, "cost_change_yuan_per_hour": None,
                "risks": ["Restart may discard non-persistent instance state"] if len(steps) > 1 else []}
    end = clock() + deadline
    started = False
    stopped = False
    restart_needed = desired == "running" and current == "3" and current_mode != mode and allow_restart
    while True:
        item = client.resolve_instance(selector)
        status = str(item.get("status"))
        live_mode = str(item.get("start_mode", mode))
        if desired == "running" and status == "3" and live_mode != mode:
            if not allow_restart:
                raise EnsureConflict("running mode conflicts with requested mode; pass --allow-restart")
            restart_needed = True
        if progress:
            progress({"state": "observe", "status": status, "desired": desired})
        if desired == "running" and status == "3" and restart_needed and not stopped:
            try:
                client.stop_instance(*client._instance_identifiers(item))
                stopped = True
            except PlatformError as exc:
                if str(exc.code) == "53524":
                    continue
                raise
        elif desired == "running" and status == "3":
            verification = {}
            if verify and verifier:
                verification = verifier(item, verify) or {}
            return {"state": "running", "instance": item, **({"verification": verification} if verification else {})}
        if desired == "stopped" and status == "5":
            return {"state": "stopped", "instance": item}
        if status in {"1", "2", "4"}:
            pass
        elif desired == "running" and status == "5" and not started:
            try:
                client.start_instance(*client._instance_identifiers(item), start_mode=mode)
                started = True
            except PlatformError as exc:
                if str(exc.code) == "53524":
                    continue
                if str(exc.code) != "53523":
                    raise
                started = True
                if progress:
                    progress({"state": "retry", "reason": "no_stock", "platform_code": "53523"})
                started = False
        elif desired == "stopped" and status != "5" and not stopped:
            try:
                client.stop_instance(*client._instance_identifiers(item))
                stopped = True
            except PlatformError as exc:
                if str(exc.code) == "53524":
                    continue
                raise
        if clock() >= end:
            raise EnsureTimeout(f"ensure {desired} deadline exceeded; last platform status={status}")
        sleep(min(30 + jitter(), max(0, end - clock())))

