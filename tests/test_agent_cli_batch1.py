import base64
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from gpufree.auth import classify, parse_token_input
from gpufree.client import _atomic_write_json
from gpufree.envelope import error_details, make_envelope, redact
from gpufree.snapshot import normalize
from gpufree.cli import main
from contextlib import redirect_stdout
from io import StringIO


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "header." + payload + ".signature"


class BatchOneContracts(unittest.TestCase):
    def test_envelope_error_codes_and_nested_redaction(self):
        self.assertEqual(make_envelope("x", {"a": 1})["schema"], 1)
        for code, expected in ((53523, 11), (53524, 13), (42408, 12)):
            self.assertEqual(error_details(type("E", (RuntimeError,), {"code": code})("failure"))[0], expected)
        self.assertEqual(redact({"nested": [{"ssh_password": "x", "refresh_token": "y", "secret_key": "z"}]}),
                         {"nested": [{"ssh_password": "[REDACTED]", "refresh_token": "[REDACTED]", "secret_key": "[REDACTED]"}]})

    def test_cookie_header_and_json_import_formats_and_bad_segments(self):
        access, identity = jwt({"exp": 9999999999}), jwt({"iss": "https://www.gpufree.cn/oidc", "exp": 9999999999})
        token_json = {"access_token": access, "refresh_token": "refresh-value", "id_token": identity}
        self.assertEqual(parse_token_input(json.dumps(token_json)), token_json)
        cookie = "; ".join(f"{key}={value}" for key, value in token_json.items())
        self.assertEqual(parse_token_input("Cookie: " + cookie), token_json)
        self.assertEqual(parse_token_input(cookie), token_json)
        for bad, fragment in ((cookie.replace("access_token=", "access_t0ken="), "access_token"),
                              (cookie.replace(access, "header.eyJleHAi.sig"), "truncated"),
                              (json.dumps({**token_json, "id_token": jwt({"iss": "https://other.invalid"})}), "iss")):
            with self.subTest(fragment=fragment), self.assertRaisesRegex(ValueError, fragment):
                parse_token_input(bad)

    def test_auth_classification_without_probe(self):
        client = type("Client", (), {"_state": {}, "_token_expiry": lambda *args: None})()
        self.assertEqual(classify(client)["classification"], "missing")
        expiry = lambda self, name: (9999999999, 0)
        client._state = {"access_token": "fixture"}
        client._token_expiry = expiry.__get__(client)
        with patch("gpufree.auth.time.time", return_value=100):
            self.assertEqual(classify(client)["classification"], "valid")

    def test_snapshot_normalization_and_warning_thresholds(self):
        data, warnings = normalize({
            "balance": {"data": {"balance": 1000}},
            "instances": {"data": {"dataList": [{"status": 3, "instance_uuid": "u", "product_name": "gpu",
                "req_gpu_amount": 1, "local_disk_info": {"root_fs_used_rate": "90%"}, "ssh_command": "ssh -p 2222 user@example"},
                {"status": 5, "instance_uuid": "old", "release_time": "2026-11-15T00:00:00+00:00"}]}}
        }, {"classification": "expiring", "remaining_seconds": 60},
            [{"product_name": "gpu", "current_price": 100, "product_skus": [{"datacenter_id": 1, "gpu_nodes_left": 2}]}],
            now=datetime(2026, 11, 14, tzinfo=timezone.utc))
        self.assertEqual(data["instances"][0]["state"], "running")
        self.assertEqual(data["instances"][0]["ssh"]["host"], "example")
        self.assertIn("disk_high", [warning["kind"] for warning in warnings])
        self.assertIn("auth_expiring", [warning["kind"] for warning in warnings])
        self.assertIn("low_balance", [warning["kind"] for warning in warnings])
        self.assertIn("release_soon", [warning["kind"] for warning in warnings])
        self.assertEqual(data["burn_yuan_per_hour"], 1)

    def test_atomic_writes_are_complete_under_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            errors = []
            def writer(value):
                try:
                    for _ in range(15):
                        _atomic_write_json(path, {"value": value, "padding": "x" * 300})
                except Exception as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(errors, [])
            self.assertIn(json.loads(path.read_text())["value"], range(4))

    def test_api_command_passes_method_path_body_to_client(self):
        output = StringIO()
        client = type("Client", (), {"_request": lambda self, method, path, body, **kw:
                                      {"code": 200, "data": {"echo": body}}})()
        with patch("sys.argv", ["gpufree", "api", "POST", "/fixture", "--body", '{"x":1}']), \
             patch("gpufree.cli.GPUFeeClient", return_value=client), redirect_stdout(output):
            self.assertEqual(main(), 0)
        envelope = json.loads(output.getvalue())
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["data"]["echo"], {"x": 1})

    def test_auth_import_no_verify_preserves_backup(self):
        from gpufree.auth import import_tokens
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "token-state.json"
            target.write_text('{"old":true}')
            client = type("Client", (), {"token_file": target, "_client_id": None})()
            result = import_tokens(client, {"access_token": "a", "refresh_token": "b", "id_token": "c"}, verify=False)
            self.assertEqual(json.loads(target.read_text())["access_token"], "a")
            self.assertEqual(json.loads(target.with_name(target.name + ".bak").read_text()), {"old": True})
            self.assertTrue(result["backup_created"])

    def test_auth_import_verification_failure_keeps_original(self):
        from gpufree.auth import import_tokens
        from gpufree.client import GPUFeeClient
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "token-state.json"
            target.write_text('{"old":true}')
            client = type("Client", (), {"token_file": target, "_client_id": None})()
            with patch.object(GPUFeeClient, "refresh_token", side_effect=RuntimeError("fixture rejection")):
                with self.assertRaisesRegex(RuntimeError, "fixture rejection"):
                    import_tokens(client, {"access_token": "a", "refresh_token": "b", "id_token": "c"}, verify=True)
            self.assertEqual(json.loads(target.read_text()), {"old": True})
            self.assertFalse(target.with_name(target.name + ".bak").exists())


if __name__ == "__main__":
    unittest.main()
