import base64
import concurrent.futures
import io
import http.client
import json
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import gpufree.client as client_module
from gpufree.client import (
    GPUFeeClient,
    _atomic_write_json,
    _b64url_decode,
    _yuan,
)


def jwt(iat: int, exp: int, **claims: object) -> str:
    payload = {"iat": iat, "exp": exp, **claims}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    return f"header.{encoded.decode()}.signature"


class FakeResponse:
    def __init__(
        self,
        body: bytes = b'{"code": 200}',
        *,
        cookies: tuple[str, ...] = (),
        events: list[str] | None = None,
    ):
        self.body = body
        self.headers = Message()
        self.events = events
        for cookie in cookies:
            self.headers.add_header("Set-Cookie", cookie)

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.events is not None:
            self.events.append("response_closed")
        return False


class ClientTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token_file = Path(self.directory.name) / "tokens.json"

    def make_client(self, state: dict) -> GPUFeeClient:
        self.token_file.write_text(json.dumps(state), encoding="utf-8")
        return GPUFeeClient(self.token_file)

    @staticmethod
    def healthy_tokens(access_token: str = "new-access") -> dict:
        now = int(time.time())
        return {
            "access_token": access_token,
            "id_token": jwt(now, now + 3600),
            "refresh_token": "refresh",
        }


class FormattingTests(ClientTestCase):
    def test_yuan_is_machine_readable_without_thousands_separator(self):
        self.assertEqual(_yuan(123456), "1234.56")
        self.assertEqual(_yuan(-123456), "-1234.56")
        self.assertEqual(_yuan(1), "0.01")
        self.assertEqual(_yuan(None), "?")

    def test_base64_padding_does_not_add_a_full_block(self):
        with patch(
            "gpufree.client.base64.urlsafe_b64decode",
            wraps=base64.urlsafe_b64decode,
        ) as decode:
            self.assertEqual(_b64url_decode("e30="), {})
        decode.assert_called_once_with("e30=")

    def test_expiry_message_uses_days_hours_and_minutes(self):
        now = 1_700_000_000
        expiry = now + 7 * 86400 + 2 * 3600 + 3 * 60 + 45
        client = self.make_client(
            {
                "access_token": jwt(now, expiry),
                "id_token": jwt(now, expiry),
                "refresh_token": "refresh",
            }
        )
        with patch("gpufree.client.time.time", return_value=now):
            status_info = client.check_expiry()
        self.assertEqual(
            status_info["message"], "Session token valid for 7d 2h 3m"
        )


class StatePersistenceTests(ClientTestCase):
    def test_atomic_write_creates_parent_and_private_file(self):
        path = Path(self.directory.name) / "nested" / "token-state.json"
        _atomic_write_json(path, {"access_token": "secret"})

        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")),
            {"access_token": "secret"},
        )
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(path.parent.glob(".gpufree-token-*")), [])

    def test_default_path_migrates_legacy_token_state(self):
        legacy = Path(self.directory.name) / "legacy-token-state.json"
        target = Path(self.directory.name) / "config" / "token-state.json"
        state = {"access_token": "old-access", "refresh_token": "refresh"}
        legacy.write_text(json.dumps(state), encoding="utf-8")

        with patch.object(client_module, "DEFAULT_TOKEN_FILE", target), patch.object(
            client_module, "_LEGACY_TOKEN_FILE", legacy
        ):
            client = GPUFeeClient()

        self.assertEqual(client.token_file, target)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), state)
        self.assertTrue(legacy.exists())

    def test_invalid_utf8_token_state_is_wrapped(self):
        self.token_file.write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(RuntimeError, "Cannot read token state"):
            GPUFeeClient(self.token_file)

    def test_save_error_is_wrapped(self):
        client = self.make_client({"access_token": "secret"})
        with patch(
            "gpufree.client._atomic_write_json",
            side_effect=OSError("read-only filesystem"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Cannot save token state"):
                client._save_state()

    def test_login_reports_atomic_write_error(self):
        output = io.StringIO()
        errors = io.StringIO()
        token_json = json.dumps(
            {"access_token": "access", "refresh_token": "refresh"}
        )
        with patch("builtins.input", return_value=token_json), patch(
            "gpufree.client._atomic_write_json",
            side_effect=OSError("read-only filesystem"),
        ), redirect_stdout(output), redirect_stderr(errors):
            exit_code = GPUFeeClient.login_interactive(self.token_file)

        self.assertEqual(exit_code, 1)
        self.assertIn("cannot save token state", errors.getvalue())


class TokenIntrospectionTests(ClientTestCase):
    def test_id_token_controls_session_status(self):
        now = int(time.time())
        client = self.make_client(
            {
                "access_token": jwt(now, now + 604800),
                "id_token": jwt(now, now - 1),
                "refresh_token": "refresh",
            }
        )
        status_info = client.check_expiry()
        self.assertEqual(status_info["status"], "refresh_required")
        self.assertEqual(
            status_info["tokens"]["access_token"]["status"], "valid"
        )
        self.assertEqual(status_info["tokens"]["id_token"]["status"], "expired")

    def test_refresh_due_uses_check_expiry_status(self):
        client = self.make_client({})
        with patch.object(
            client, "check_expiry", return_value={"status": "refresh_due"}
        ):
            self.assertTrue(client._refresh_due())

    def test_client_id_source_reports_all_resolution_paths(self):
        now = int(time.time())
        explicit = GPUFeeClient(self.token_file, client_id="explicit-id")
        self.assertEqual((explicit.client_id, explicit.client_id_source), (
            "explicit-id",
            "explicit argument",
        ))

        extracted = self.make_client(
            {"id_token": jwt(now, now + 3600, client_id="jwt-id")}
        )
        self.assertEqual((extracted.client_id, extracted.client_id_source), (
            "jwt-id",
            "stored JWT",
        ))

        fallback = self.make_client({})
        self.assertEqual(fallback.client_id_source, "built-in default")


class RequestTests(ClientTestCase):
    def test_missing_access_token_is_recovered_with_refresh_token(self):
        client = self.make_client({"refresh_token": "refresh"})

        def rotate_tokens():
            client._state.update(self.healthy_tokens())
            return client._state.copy()

        with patch.object(
            client, "_refresh_token_unlocked", side_effect=rotate_tokens
        ) as refresh, patch(
            "gpufree.client.urllib.request.urlopen",
            return_value=FakeResponse(),
        ) as urlopen:
            client._get("/test")

        refresh.assert_called_once_with()
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer new-access")

    def test_invalid_json_is_wrapped_for_html_and_invalid_utf8(self):
        client = self.make_client({"access_token": "opaque"})
        for body in (b"<html>gateway error</html>", b"\xff\xfe\xfa"):
            with self.subTest(body=body), patch(
                "gpufree.client.urllib.request.urlopen",
                return_value=FakeResponse(body),
            ):
                with self.assertRaisesRegex(RuntimeError, "returned invalid JSON"):
                    client._get("/test")

    def test_http_401_refreshes_and_retries_once(self):
        client = self.make_client(
            {"access_token": "old-access", "refresh_token": "refresh"}
        )
        http_error = urllib.error.HTTPError(
            "https://example.test",
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b"{}"),
        )

        def rotate_tokens():
            client._state.update(self.healthy_tokens())
            return client._state.copy()

        with patch.object(
            client, "_refresh_token_unlocked", side_effect=rotate_tokens
        ) as refresh, patch(
            "gpufree.client.urllib.request.urlopen",
            side_effect=[http_error, FakeResponse(b'{"ok": true}')],
        ) as urlopen:
            result = client._get("/test")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        refresh.assert_called_once_with()

    def test_code_43003_closes_response_before_refresh_and_retry(self):
        client = self.make_client(
            {"access_token": "old-access", "refresh_token": "refresh"}
        )
        events: list[str] = []

        def rotate_tokens():
            events.append("refresh")
            client._state.update(self.healthy_tokens())
            return client._state.copy()

        with patch.object(
            client, "_refresh_token_unlocked", side_effect=rotate_tokens
        ) as refresh, patch(
            "gpufree.client.urllib.request.urlopen",
            side_effect=[
                FakeResponse(b'{"code": "43003"}', events=events),
                FakeResponse(b'{"ok": true}', events=events),
            ],
        ) as urlopen:
            result = client._get("/test")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(events[:2], ["response_closed", "refresh"])
        self.assertEqual(urlopen.call_count, 2)
        refresh.assert_called_once_with()

    def test_code_43003_after_refresh_does_not_retry_again(self):
        client = self.make_client(
            {"access_token": "old-access", "refresh_token": "refresh"}
        )

        def rotate_tokens():
            client._state.update(self.healthy_tokens())
            return client._state.copy()

        denied = FakeResponse(b'{"code": 43003}')
        with patch.object(
            client, "_refresh_token_unlocked", side_effect=rotate_tokens
        ) as refresh, patch(
            "gpufree.client.urllib.request.urlopen",
            side_effect=[denied, denied],
        ) as urlopen:
            with self.assertRaisesRegex(RuntimeError, "after token refresh"):
                client._get("/test")

        self.assertEqual(urlopen.call_count, 2)
        refresh.assert_called_once_with()

    def test_get_timeout_waits_before_single_retry(self):
        client = self.make_client({"access_token": "opaque"})
        with patch(
            "gpufree.client.urllib.request.urlopen",
            side_effect=[
                urllib.error.URLError(TimeoutError("timed out")),
                FakeResponse(b'{"ok": true}'),
            ],
        ) as urlopen, patch("gpufree.client.time.sleep") as sleep:
            result = client._get("/test")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_get_retries_remote_disconnect_but_post_does_not(self):
        client = self.make_client({"access_token": "opaque"})
        with patch(
            "gpufree.client.urllib.request.urlopen",
            side_effect=[
                urllib.error.URLError(http.client.RemoteDisconnected("closed")),
                FakeResponse(b'{"ok": true}'),
            ],
        ) as urlopen, patch("gpufree.client.time.sleep") as sleep:
            self.assertEqual(client._get("/test"), {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

        with patch(
            "gpufree.client.urllib.request.urlopen",
            side_effect=urllib.error.URLError(http.client.RemoteDisconnected("closed")),
        ) as urlopen, patch("gpufree.client.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "unreachable"):
                client._request("POST", "/test", {})
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()


class InstanceControlTests(ClientTestCase):
    item = {"instance_id": 12, "instance_uuid": "uuid-12", "nick_name": "worker"}
    webide_item = {
        "webide_instance_id": 12345,
        "webide_instance_uuid": "fakeuuid-abcdef12",
        "webide_instance_name": "jupyter-fakeuuid-abcdef12",
        "nick_name": "",
        "status": 5,
        "charge_type": 0,
        "product_name": "test-product",
        "data_center_id": 1,
    }

    def test_webide_resolution(self):
        client = self.make_client({})
        with patch.object(client, "list_instances", return_value={
            "data": {"dataList": [self.webide_item]}
        }):
            for selector in (None, "12345", "fakeuuid-abcdef12", "jupyter-fakeuuid-abcdef12"):
                with self.subTest(selector=selector):
                    self.assertEqual(client.resolve_instance(selector), self.webide_item)
            with self.assertRaisesRegex(RuntimeError, "found 0"):
                client.resolve_instance("")

    def test_webide_start_stop_by_selector(self):
        client = self.make_client({"access_token": "fake"})
        for action in ("start", "stop"):
            for selector in ("12345", "fakeuuid-abcdef12", "jupyter-fakeuuid-abcdef12"):
                with self.subTest(action=action, selector=selector), patch.object(
                    client, "list_instances", return_value={"data": {"dataList": [self.webide_item]}}
                ), patch("gpufree.client.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
                    getattr(client, action + "_instance_by_selector")(selector)
                    req = urlopen.call_args.args[0]
                    self.assertEqual(req.method, "PUT")
                    body = json.loads(req.data)
                    expected = {"instance_id": 12345, "instance_uuid": "fakeuuid-abcdef12",
                                "action": action}
                    if action == "start":
                        expected["start_mode"] = "gpu"
                    self.assertEqual(body, expected)
                    self.assertIs(type(body["instance_id"]), int)

    def test_json_requests_and_form_default(self):
        client = self.make_client({"access_token": "fake"})
        for action, mode in (("start", "gpu"), ("start", "nogpu"), ("stop", None)):
            with self.subTest(action=action, mode=mode), patch(
                "gpufree.client.urllib.request.urlopen", return_value=FakeResponse()
            ) as urlopen:
                if action == "start":
                    client.start_instance(12, "uuid-12", start_mode=mode)
                else:
                    client.stop_instance(12, "uuid-12")
                req = urlopen.call_args.args[0]
                self.assertEqual(req.method, "PUT")
                self.assertEqual(req.full_url, "https://www.gpufree.cn/api/v1/inferring-api/webide/")
                self.assertEqual(req.get_header("Content-type"), "application/json")
                expected = {"instance_id": 12, "instance_uuid": "uuid-12", "action": action}
                if mode:
                    expected["start_mode"] = mode
                self.assertEqual(json.loads(req.data), expected)
        with patch("gpufree.client.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            client._request("POST", "/fake", {"a": "b c"})
        self.assertEqual(urlopen.call_args.args[0].data, b"a=b+c")
        self.assertEqual(urlopen.call_args.args[0].get_header("Content-type"),
                         "application/x-www-form-urlencoded")

    def test_invalid_start_mode(self):
        client = self.make_client({})
        with patch.object(client, "list_instances") as listing:
            for method, args in ((client.start_instance, (12, "uuid")),
                                 (client.start_instance_by_selector, ())):
                with self.assertRaises(ValueError):
                    method(*args, start_mode="invalid")
            listing.assert_not_called()

    def test_resolution(self):
        client = self.make_client({})
        for item in (self.item, {"id": 12, "uuid": "uuid-12", "nick_name": "worker"}):
            with patch.object(client, "list_instances", return_value={"data": {"dataList": [item]}}):
                for selector in (None, "12", "uuid-12", "worker"):
                    with self.subTest(item=item, selector=selector):
                        self.assertEqual(client.resolve_instance(selector), item)
                with self.assertRaisesRegex(RuntimeError, "Candidates: worker.*12.*uuid-12"):
                    client.resolve_instance("missing")

    def test_resolution_errors_and_odd_shapes(self):
        client = self.make_client({})
        for response in ({}, None, {"data": None}, {"data": {"dataList": "bad"}},
                         {"data": {"dataList": [None, 42]}},
                         {"data": {"dataList": [self.item, self.item]}}):
            with self.subTest(response=response), patch.object(client, "list_instances", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "Candidates:"):
                    client.resolve_instance(None)
        with patch.object(client, "list_instances", return_value={"data": {"dataList": [self.item, self.item]}}):
            with self.assertRaisesRegex(RuntimeError, "found 2"):
                client.resolve_instance("worker")
        with patch.object(client, "list_instances", return_value={"data": {"dataList": [{"id": 12, "nick_name": "worker"}]}}):
            with self.assertRaisesRegex(RuntimeError, "present keys: id, nick_name"):
                client.resolve_instance(None)

    def test_wait_transitions(self):
        client = self.make_client({})
        for action, target in (("start", 3), ("stop", 5)):
            final = dict(self.item, status=target)
            responses = [{"data": {"dataList": [item]}} for item in
                         (self.item, dict(self.item, status=1), final)]
            with self.subTest(action=action), patch.object(client, "list_instances", side_effect=responses), patch.object(
                client, action + "_instance", return_value={"code": 200}
            ) as request, patch("gpufree.client.time.sleep") as sleep:
                result = getattr(client, action + "_instance_by_selector")(wait=True)
                self.assertEqual(result, {"response": {"code": 200}, "instance": final})
                sleep.assert_called_once_with(5)
                self.assertEqual(request.call_args.args, (12, "uuid-12"))

    def test_wait_timeout_and_unavailable_status(self):
        client = self.make_client({})
        for status, error in (({"status": 7}, "last seen status: 7"), ({}, "status field is unavailable")):
            with self.subTest(status=status), patch.object(client, "list_instances", return_value={
                "data": {"dataList": [dict(self.item, **status)]}
            }), patch.object(client, "stop_instance", return_value={}), patch("gpufree.client.time.sleep") as sleep:
                with self.assertRaisesRegex(RuntimeError, error):
                    client.stop_instance_by_selector(wait=True, wait_timeout=0)
                sleep.assert_not_called()

    def test_no_wait_returns_raw_response(self):
        client = self.make_client({})
        for action in ("start", "stop"):
            with patch.object(client, "resolve_instance", return_value=self.item), patch.object(
                client, action + "_instance", return_value={"code": 200}
            ), patch.object(client, "_wait_for_instance") as wait:
                self.assertEqual(getattr(client, action + "_instance_by_selector")(), {"code": 200})
                wait.assert_not_called()


class RefreshTests(ClientTestCase):
    def test_refresh_accepts_string_success_code(self):
        client = self.make_client({"refresh_token": "old-refresh"})
        response = FakeResponse(
            b'{"code": "200"}',
            cookies=("access_token=new-access; Path=/; HttpOnly",),
        )
        with patch(
            "gpufree.client.urllib.request.urlopen", return_value=response
        ), patch.object(client, "_save_state"):
            state = client.refresh_token()
        self.assertEqual(state["access_token"], "new-access")

    def test_refresh_wraps_invalid_utf8(self):
        client = self.make_client({"refresh_token": "refresh"})
        with patch(
            "gpufree.client.urllib.request.urlopen",
            return_value=FakeResponse(b"\xff\xfe"),
        ):
            with self.assertRaisesRegex(RuntimeError, "returned invalid JSON"):
                client.refresh_token()

    def test_refresh_drops_stale_id_token_when_server_does_not_rotate_it(self):
        now = int(time.time())
        client = self.make_client(
            {
                "access_token": jwt(now, now + 60),
                "id_token": jwt(now, now - 1),
                "refresh_token": "refresh",
            }
        )
        response = FakeResponse(
            b'{"code": 200}',
            cookies=("access_token=new-access; Path=/; HttpOnly",),
        )
        with patch(
            "gpufree.client.urllib.request.urlopen", return_value=response
        ), patch.object(client, "_save_state"):
            state = client.refresh_token()

        self.assertNotIn("id_token", state)
        self.assertEqual(state["access_token"], "new-access")

    def test_concurrent_auth_failures_rotate_token_once(self):
        client = self.make_client(
            {"access_token": "old-access", "refresh_token": "refresh"}
        )
        barrier = threading.Barrier(5)

        def rotate_tokens():
            client._state.update(self.healthy_tokens())
            return client._state.copy()

        def reject_old_token():
            barrier.wait(timeout=2)
            client._refresh_after_auth_failure("old-access")

        with patch.object(
            client, "_refresh_token_unlocked", side_effect=rotate_tokens
        ) as refresh, concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(reject_old_token) for _ in range(5)]
            for future in futures:
                future.result(timeout=3)

        refresh.assert_called_once_with()


class StatusTests(ClientTestCase):
    def test_status_requests_run_concurrently_and_tolerate_null_disk_info(self):
        client = self.make_client({"access_token": "opaque"})
        barrier = threading.Barrier(5)
        responses = {
            "get_userinfo": {"phone": "test"},
            "get_balance_overview": {
                "data": {"balance": 123456, "amount": 200, "cost": 50}
            },
            "list_instances": {
                "data": {
                    "dataList": [
                        {"nick_name": "null-disk", "local_disk_info": None},
                        {"nick_name": "bad-disk", "local_disk_info": "invalid"},
                    ]
                }
            },
            "get_dashboard_instances": {"data": {}},
            "get_unread_count": {"data": 0},
        }

        def concurrent_response(value: dict):
            barrier.wait(timeout=2)
            return value

        with ExitStack() as stack:
            for method_name, response in responses.items():
                stack.enter_context(
                    patch.object(
                        client,
                        method_name,
                        side_effect=lambda value=response: concurrent_response(value),
                    )
                )
            result = client.get_status()

        self.assertEqual(result["balance_summary"]["balance_yuan"], "1234.56")
        self.assertEqual(
            [item["disk_root"] for item in result["instances_summary"]],
            ["?", "?"],
        )

    def test_status_preflight_refreshes_only_once(self):
        now = int(time.time())
        client = self.make_client(
            {
                "access_token": jwt(now, now + 60),
                "id_token": jwt(now, now + 60),
                "refresh_token": "refresh",
            }
        )

        def rotate_tokens():
            client._state.update(self.healthy_tokens())
            return client._state.copy()

        method_names = (
            "get_userinfo",
            "get_balance_overview",
            "list_instances",
            "get_dashboard_instances",
            "get_unread_count",
        )
        with patch.object(
            client, "_refresh_token_unlocked", side_effect=rotate_tokens
        ) as refresh, ExitStack() as stack:
            for method_name in method_names:
                stack.enter_context(patch.object(client, method_name, return_value={}))
            client.get_status()

        refresh.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
