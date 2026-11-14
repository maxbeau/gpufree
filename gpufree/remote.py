"""Password based OpenSSH transport with an isolated known-hosts file."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


class SSHTransport:
    def __init__(self, config_dir, *, runner=None):
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.known_hosts = self.config_dir / "known_hosts_gpufree"
        self.runner = runner or subprocess.run

    @staticmethod
    def endpoint(item):
        command = item.get("ssh_command") or item.get("ssh") or {}
        if isinstance(command, str):
            tokens = shlex.split(command)
            try:
                ssh_index = next(i for i, token in enumerate(tokens) if Path(token).name == "ssh")
            except StopIteration:
                ssh_index = -1
            port, user, host = 22, "root", None
            i = ssh_index + 1
            while i < len(tokens):
                token = tokens[i]
                if token in {"-p", "-l"} and i + 1 < len(tokens):
                    value = tokens[i + 1]
                    if token == "-p": port = int(value)
                    else: user = value
                    i += 2
                    continue
                if token.startswith("-"):
                    i += 2 if token in {"-o", "-i", "-F", "-J", "-W"} else 1
                    continue
                host = token
                break
            if host:
                if "@" in host:
                    user, host = host.rsplit("@", 1)
                return host, port, user, item.get("ssh_password")
        if isinstance(command, dict):
            return (command.get("host"), int(command.get("port") or 22),
                    command.get("user") or "root", item.get("ssh_password") or command.get("password"))
        raise RuntimeError("instance SSH endpoint is unavailable")

    def invoke(self, item, args, *, via=None, timeout=None, stdin=None, tty=False,
               accept_new_hostkey=False, capture=False, forward=None):
        binary = shutil.which("ssh")
        if not binary:
            raise RuntimeError("OpenSSH client is required for instance remote access")
        host, port, user, password = self.endpoint(item)
        if not host or not password:
            raise RuntimeError("instance SSH host or password is unavailable")
        self.known_hosts.touch(mode=0o600, exist_ok=True)
        os.chmod(self.known_hosts, 0o600)
        opts = ["-o", "BatchMode=no", "-o", "PreferredAuthentications=password,keyboard-interactive",
                "-o", "PubkeyAuthentication=no", "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={self.known_hosts}", "-p", str(port)]
        if accept_new_hostkey:
            self.runner(["ssh-keygen", "-R", f"[{host}]:{port}", "-f", str(self.known_hosts)],
                        capture_output=True, text=True)
            opts[opts.index("accept-new")] = "yes"
        if via:
            opts.extend(["-J", via])
        if forward:
            opts.extend(["-L", forward, "-N"])
        if tty:
            opts.append("-t")
        with tempfile.TemporaryDirectory(prefix="gpufree-askpass-") as directory:
            script = Path(directory) / "askpass"
            script.write_text("#!/bin/sh\nprintf '%s\\n' \"$GPUFREE_SSH_PASSWORD\"\n")
            script.chmod(0o700)
            env = os.environ.copy()
            env.update({"SSH_ASKPASS": str(script), "SSH_ASKPASS_REQUIRE": "force",
                        "GPUFREE_SSH_PASSWORD": password, "DISPLAY": env.get("DISPLAY", ":0")})
            command = [binary, *opts, f"{user}@{host}", *args]
            try:
                result = self.runner(command, input=stdin, timeout=timeout, env=env,
                                     text=False, check=False, capture_output=capture)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("SSH command timed out") from exc
            finally:
                env.pop("GPUFREE_SSH_PASSWORD", None)
            return result if capture else result.returncode

