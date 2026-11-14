"""Local safety primitives and deterministic helpers for agent operations."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from gpufree.envelope import redact


def state_dir(token_file):
    return Path(token_file).expanduser().parent


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)

    def records(self):
        try:
            return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]
        except FileNotFoundError:
            return []

    def append(self, record):
        record = redact(dict(record))
        record.setdefault("at", datetime.now(timezone.utc).isoformat())
        data = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(self.path, 0o600)

    def intent(self, operation, target, parameters, key=None, identity=None):
        digest = hashlib.sha256(json.dumps(redact(parameters), sort_keys=True, default=str).encode()).hexdigest()
        event_id = str(uuid.uuid4())
        self.append({"type": "intent", "id": event_id, "operation": operation, "target": target,
                     "parameters_hash": digest, "idempotency_key": key,
                     "identity": redact(identity) if identity else None,
                     "executor": {"host": socket.gethostname(), "pid": os.getpid()}, "epoch": time.time()})
        return event_id

    def result(self, event_id, result=None, error=None):
        self.append({"type": "result", "id": event_id, "result": redact(result),
                     "error": str(error) if error else None})

    def successful_key(self, operation, key):
        rows = self.records()
        results = {row.get("id"): row for row in rows if row.get("type") == "result" and not row.get("error")}
        for row in reversed(rows):
            if row.get("type") == "intent" and row.get("operation") == operation and row.get("idempotency_key") == key:
                return results.get(row.get("id"), {}).get("result")
        return None

    def open_intents(self, operation=None, target=None):
        rows = self.records()
        closed = {r.get("id") for r in rows if r.get("type") == "result"}
        return [r for r in rows if r.get("type") == "intent" and r.get("id") not in closed
                and (operation is None or r.get("operation") == operation)
                and (target is None or r.get("target") == target)]


@contextmanager
def operation_lock(directory, selector, journal):
    safe = hashlib.sha256(str(selector).encode()).hexdigest()[:24]
    path = Path(directory) / ("lock-" + safe)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stream = path.open("a+")
    os.chmod(path, 0o600)
    try:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = journal.open_intents(target=str(selector))
            raise RuntimeError("operation lock held; journal=" + json.dumps(holder[-1] if holder else {}, ensure_ascii=False)) from exc
        yield
    finally:
        stream.close()


def context_path(token_file):
    return state_dir(token_file) / "context.json"


def read_context(token_file):
    try:
        value = json.loads(context_path(token_file).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_context(token_file, selector):
    path = context_path(token_file)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".context-")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump({"instance": selector}, out)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def register_ttl(selector, token_file, ttl_seconds, *, runner=None, now=None):
    """Schedule a detached stop using systemd-run, then crontab; raise if unavailable."""
    import shlex
    import shutil
    import subprocess

    runner = runner or subprocess.run
    now = time.time() if now is None else now
    due = datetime.fromtimestamp(now + ttl_seconds, timezone.utc)
    unit = "gpufree-ttl-" + hashlib.sha256(f"{selector}:{now}".encode()).hexdigest()[:12]
    command = ["gpufree", "instance", "ensure", "stopped", str(selector)]
    if shutil.which("systemd-run"):
        result = runner(["systemd-run", "--user", "--unit", unit, "--on-active", f"{ttl_seconds}s",
                         "--collect", *command], capture_output=True, text=True)
        if result.returncode == 0:
            return {"id": unit, "expires_at": due.isoformat(), "scheduler": "systemd-run"}
    crontab = shutil.which("crontab")
    if crontab:
        existing = runner([crontab, "-l"], capture_output=True, text=True)
        text = existing.stdout if existing.returncode == 0 else ""
        marker = "gpufree-ttl:" + unit
        one_shot = shlex.join(command) + f"; (crontab -l 2>/dev/null | grep -Fv '{marker}' | crontab -)"
        line = f"{due.minute} {due.hour} {due.day} {due.month} * sh -c {shlex.quote(one_shot)} # {marker}"
        updated = (text.rstrip() + "\n" + line + "\n").lstrip()
        result = runner([crontab, "-"], input=updated, capture_output=True, text=True)
        if result.returncode == 0:
            return {"id": unit, "expires_at": due.isoformat(), "scheduler": "crontab"}
    raise RuntimeError("TTL registration failed: systemd-run and crontab unavailable or rejected the job")

