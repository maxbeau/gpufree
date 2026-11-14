import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from gpufree.operations import Journal, operation_lock, register_ttl, read_context, write_context
from gpufree.reconcile import ensure, EnsureConflict, EnsureTimeout
from gpufree.remote import SSHTransport
from gpufree.client import PlatformError
from gpufree.cli import main
from gpufree.envelope import error_details


class BatchTwoContracts(unittest.TestCase):
    def test_network_failures_are_retryable_cli_errors(self):
        import http.client
        self.assertEqual(error_details(http.client.RemoteDisconnected("closed"))[0], 14)

    def test_selftest_accepts_top_level_oidc_userinfo(self):
        class FakeClient:
            def get_userinfo(self): return {"sub": "user-1", "user_info": {}}
            def get_balance(self): return {"data": {"balance": 100}}
            def list_instances(self): return {"data": {"dataList": []}}
            def list_products(self): return {"data": []}
        with tempfile.TemporaryDirectory() as directory:
            output = StringIO()
            with patch("sys.argv", ["gpufree", "--token-file", str(Path(directory) / "token"), "selftest"]), \
                 patch("gpufree.cli.GPUFeeClient", return_value=FakeClient()), redirect_stdout(output):
                self.assertEqual(main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual([item["status"] for item in result["data"]["checks"]], ["ok"] * 4)

    def test_cli_state_uses_explicit_token_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_dir = root / "alternate"
            config_home = root / "monitored-xdg"
            token_dir.mkdir()
            token_file = token_dir / "tokens.json"
            token_file.write_text(json.dumps({"access_token": "synthetic"}))
            default_config = config_home / "gpufree"
            output = StringIO()
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}), \
                 patch("gpufree.cli.DEFAULT_TOKEN_FILE", default_config / "token-state.json"), \
                 patch("gpufree.client.DEFAULT_TOKEN_FILE", default_config / "token-state.json"), \
                 patch("sys.argv", ["gpufree", "--token-file", str(token_file), "instance", "ensure", "running", "worker"]), \
                 patch("gpufree.cli.reconcile_ensure", return_value={"state": "running", "warnings": []}), \
                 redirect_stdout(output):
                self.assertEqual(main(), 0)
            self.assertTrue((token_dir / "journal.jsonl").exists())
            self.assertTrue(any(path.name.startswith("lock-") for path in token_dir.iterdir()))
            self.assertFalse(default_config.exists())

    def test_journal_intents_results_and_idempotency(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / "journal.jsonl")
            event = journal.intent("create", "worker", {"name": "worker", "ssh_password": "hidden"}, "key")
            self.assertIsNone(journal.successful_key("create", "key"))
            journal.result(event, {"id": 7, "ssh_password": "secret"})
            self.assertEqual(journal.successful_key("create", "key"), {"id": 7, "ssh_password": "[REDACTED]"})
            self.assertEqual(journal.open_intents(), [])
            self.assertNotIn("hidden", Path(directory, "journal.jsonl").read_text())

    def test_selector_lock_contention_and_context(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / "journal.jsonl")
            write_context(Path(directory) / "token", "uuid-1")
            self.assertEqual(read_context(Path(directory) / "token")["instance"], "uuid-1")
            with operation_lock(directory, "uuid-1", journal):
                with self.assertRaisesRegex(RuntimeError, "operation lock held"):
                    with operation_lock(directory, "uuid-1", journal):
                        pass

    def test_reconcile_retries_stock_and_does_not_duplicate_pending_start(self):
        item_stopped = {"status": 5, "start_mode": "gpu", "instance_id": 1, "instance_uuid": "u"}
        item_running = {**item_stopped, "status": 3}
        class Fake:
            def __init__(self): self.reads = 0; self.starts = 0
            def resolve_instance(self, selector):
                self.reads += 1
                return item_stopped if self.reads < 4 else item_running
            def _instance_identifiers(self, item): return item["instance_id"], item["instance_uuid"]
            def start_instance(self, *args, **kwargs):
                self.starts += 1
                if self.starts == 1: raise PlatformError(53523, "stock")
                return {}
        fake = Fake()
        result = ensure(fake, "u", "running", deadline=20, sleep=lambda _: None,
                        clock=iter([0, 0, 0, 0, 0, 0, 0]).__next__, jitter=lambda: 0)
        self.assertEqual(result["state"], "running")
        self.assertEqual(fake.starts, 2)

    def test_plan_is_read_only_conflicts_and_deadline(self):
        item = {"status": 5, "start_mode": "gpu", "instance_id": 1, "instance_uuid": "u"}
        class Fake:
            starts = 0
            stops = 0
            def resolve_instance(self, selector): return item
            def _instance_identifiers(self, value): return 1, "u"
            def start_instance(self, *args, **kwargs): self.starts += 1
            def stop_instance(self, *args): self.stops += 1
        fake = Fake()
        self.assertEqual(ensure(fake, "u", "running", plan_only=True)["plan"], [{"action": "start", "mode": "gpu"}])
        self.assertEqual((fake.starts, fake.stops), (0, 0))
        with self.assertRaises(EnsureTimeout):
            ensure(fake, "u", "running", deadline=0, sleep=lambda _: None, clock=lambda: 0)
        item["status"] = 3; item["start_mode"] = "nogpu"
        with self.assertRaises(EnsureConflict): ensure(fake, "u", "running", mode="gpu")

    def test_ssh_password_only_in_child_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            observed = {}
            class Result: returncode = 23
            def runner(argv, **kwargs):
                observed.update(argv=argv, env=dict(kwargs["env"]), capture=kwargs.get("capture_output"))
                return Result()
            transport = SSHTransport(directory, runner=runner)
            item = {"ssh_command": "ssh -o ConnectTimeout=5 -p 2202 root@host.invalid", "ssh_password": "sensitive"}
            self.assertEqual(transport.endpoint(item)[:3], ("host.invalid", 2202, "root"))
            self.assertEqual(transport.invoke(item, ["printf", "ok"], capture=True).returncode, 23)
            self.assertNotIn("sensitive", observed["argv"])
            self.assertEqual(observed["env"]["GPUFREE_SSH_PASSWORD"], "sensitive")
            self.assertTrue(observed["env"]["SSH_ASKPASS"].endswith("/askpass"))

    def test_reconcile_53524_rereads_state_without_duplicate_action(self):
        stopped = {"status": 5, "start_mode": "gpu", "instance_id": 1, "instance_uuid": "u"}
        running = {**stopped, "status": 3}
        class Fake:
            reads = 0
            starts = 0
            def resolve_instance(self, selector):
                self.reads += 1
                return stopped if self.reads < 3 else running
            def _instance_identifiers(self, item): return 1, "u"
            def start_instance(self, *args, **kwargs):
                self.starts += 1
                raise PlatformError(53524, "state moved")
        client = Fake()
        outcome = ensure(client, "u", "running", deadline=10, sleep=lambda _: None, clock=lambda: 0)
        self.assertEqual(outcome["state"], "running")
        self.assertEqual((client.reads, client.starts), (3, 1))

    def test_ttl_uses_systemd_then_cron_fallback(self):
        with patch("shutil.which", side_effect=lambda name: "/usr/bin/" + name if name == "systemd-run" else None):
            result = register_ttl("worker", "/tmp/token", 3600,
                                  runner=lambda *a, **k: type("R", (), {"returncode": 0})(), now=0)
        self.assertEqual(result["scheduler"], "systemd-run")
        self.assertEqual(result["expires_at"], "1970-01-01T01:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
