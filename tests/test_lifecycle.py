"""Lifecycle coverage using synthetic instances, credentials, and HTTP responses."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from gpufree.cli import main
from gpufree.client import BASE_URL, GPUFeeClient, PlatformError


SOURCE = {
    "webide_instance_id": 42,
    "webide_instance_uuid": "abcde-0123456789",
    "nick_name": "worker",
    "status": 5,
    "product_sku_code": "test-sku",
    "data_center_id": 7,
    "req_gpu_amount": 2,
}
CREATE_ARGS = {"product_sku_code": "test-sku", "data_center_id": 7, "image_id": 19}
SUCCESS = {"code": 200, "data": {"instance_uuid": "new-uuid"}}


def response(payload):
    result = MagicMock()
    result.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return result


class LifecycleTestCase(unittest.TestCase):
    def setUp(self):
        # Never construct a client or open a real token file.
        self.client = object.__new__(GPUFeeClient)
        network = patch("gpufree.client.urllib.request.urlopen",
                        side_effect=AssertionError("unmocked network"))
        network.start()
        self.addCleanup(network.stop)
        tokens = patch.object(self.client, "_ensure_access_token", return_value="fixture-token")
        tokens.start()
        self.addCleanup(tokens.stop)

    def cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["gpufree", *argv]), patch(
            "gpufree.cli.GPUFeeClient", wraps=GPUFeeClient, return_value=self.client
        ), redirect_stdout(out), redirect_stderr(err):
            code = main()
        return code, out.getvalue(), err.getvalue()

    def assert_json_request(self, request, method, path, payload):
        self.assertEqual(request.method, method)
        self.assertEqual(request.full_url, BASE_URL + path)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), payload)


class LifecycleClientTests(LifecycleTestCase):
    def test_create_default_json_endpoint_and_payload(self):
        with patch("gpufree.client.urllib.request.urlopen", return_value=response(SUCCESS)) as http:
            self.assertEqual(self.client.create_instance(**CREATE_ARGS), SUCCESS)
        http.assert_called_once()
        self.assert_json_request(http.call_args.args[0], "POST", "/api/v1/inferring-api/webide", {
            **CREATE_ARGS, "charge_type": 0, "req_amount": 1, "start_model": "gpu",
            "expand_data_disk": 0, "duration_type": "", "duration_count": 1,
        })

    def test_create_all_overrides_and_gib_conversion(self):
        with patch("gpufree.client.urllib.request.urlopen", return_value=response(SUCCESS)) as http:
            self.client.create_instance(**CREATE_ARGS, charge_type=1, req_amount=4,
                                        start_model="nogpu", expand_data_disk_gb=3,
                                        duration_type="month", duration_count=2)
        self.assert_json_request(http.call_args.args[0], "POST", "/api/v1/inferring-api/webide", {
            **CREATE_ARGS, "charge_type": 1, "req_amount": 4, "start_model": "nogpu",
            "expand_data_disk": 3 * 1024**3, "duration_type": "month", "duration_count": 2,
        })

    def test_create_validation_prevents_requests(self):
        for options in ({"start_model": "cpu"}, {"start_model": "GPU"}, {"start_model": []},
                        {"charge_type": -1}, {"charge_type": 2}, {"charge_type": "0"},
                        {"charge_type": []}, {"req_amount": 0}, {"req_amount": -2},
                        {"req_amount": None}, {"req_amount": "2"}, {"req_amount": float("nan")}):
            with self.subTest(options=options), patch.object(self.client, "_request") as request:
                with self.assertRaises(ValueError):
                    self.client.create_instance(**CREATE_ARGS, **options)
                request.assert_not_called()

    def test_clone_default_full_lookup_quota_and_patch(self):
        replies = [response({"code": 200, "data": {"dataList": [SOURCE]}}),
                   response({"code": 200, "data": {"clone_count_left": 4}}),
                   response(SUCCESS)]
        with patch("gpufree.client.urllib.request.urlopen", side_effect=replies) as http:
            self.assertEqual(self.client.clone_instance_by_selector("worker"), SUCCESS)
        self.assertEqual(http.call_count, 3)
        lookup, quota, mutation = [call.args[0] for call in http.call_args_list]
        self.assertEqual(lookup.method, "GET")
        self.assertEqual(lookup.full_url, BASE_URL + "/api/v1/jupyter/list_instance_pages?page_no=1&page_size=200")
        self.assertEqual(quota.method, "GET")
        self.assertEqual(quota.full_url, BASE_URL + "/api/v1/jupyter/left_copy")
        self.assert_json_request(mutation, "PATCH", "/api/v1/inferring-api/webide/", {
            "nickname": "worker 副本", "charge_type": 0, "clone_data": 0,
            "data_center_id": 7, "duration_count": 1, "duration_type": "day",
            "expand_data_disk": 0, "product_sku_code": "test-sku", "req_amount": 2,
            "source_webide_id": 42,
        })

    def test_clone_overrides_and_gib_conversion(self):
        with patch.object(self.client, "resolve_instance", return_value=SOURCE) as resolve, patch.object(
            self.client, "get_instance_left_copy", return_value={"data": {"clone_count_left": 1}}
        ), patch("gpufree.client.urllib.request.urlopen", return_value=response(SUCCESS)) as http:
            self.client.clone_instance_by_selector("42", nickname="copy", clone_data=True,
                                                   charge_type=1, expand_data_disk_gb=6,
                                                   duration_type="month", duration_count=3,
                                                   product_sku_code="other", data_center_id=9,
                                                   req_amount=4)
        resolve.assert_called_once_with("42")
        self.assert_json_request(http.call_args.args[0], "PATCH", "/api/v1/inferring-api/webide/", {
            "nickname": "copy", "charge_type": 1, "clone_data": 1,
            "data_center_id": 9, "duration_count": 3, "duration_type": "month",
            "expand_data_disk": 6 * 1024**3, "product_sku_code": "other", "req_amount": 4,
            "source_webide_id": 42,
        })
        self.assertIs(type(json.loads(http.call_args.args[0].data)["clone_data"]), int)

    def test_clone_empty_name_defaults_to_uuid_and_string_stopped_status(self):
        source = dict(SOURCE, nick_name="", status="5")
        with patch.object(self.client, "resolve_instance", return_value=source) as resolve, patch.object(
            self.client, "get_instance_left_copy", return_value={"data": {"clone_count_left": 1}}
        ), patch.object(self.client, "_request", return_value=SUCCESS) as request:
            self.client.clone_instance_by_selector()
        resolve.assert_called_once_with(None)
        self.assertEqual(request.call_args.args[2]["nickname"], "abcde-0123456789 副本")

    def test_clone_source_req_amount_field_is_preserved_when_present(self):
        with patch.object(self.client, "resolve_instance", return_value=dict(SOURCE, req_amount=3)), patch.object(
            self.client, "get_instance_left_copy", return_value={"data": {"clone_count_left": 1}}
        ), patch.object(self.client, "_request", return_value=SUCCESS) as request:
            self.client.clone_instance_by_selector()
        self.assertEqual(request.call_args.args[2]["req_amount"], 3)

    def test_clone_rejects_non_stopped_or_missing_status_before_quota_or_patch(self):
        for status in (1, 3, "3", None, 99):
            source = dict(SOURCE, status=status)
            with self.subTest(status=status), patch.object(
                self.client, "resolve_instance", return_value=source
            ), patch.object(self.client, "get_instance_left_copy") as quota, patch.object(
                self.client, "_request"
            ) as request:
                with self.assertRaisesRegex(RuntimeError, "关机.*stop"):
                    self.client.clone_instance_by_selector()
                quota.assert_not_called()
                request.assert_not_called()

    def test_clone_exhausted_quota_prevents_patch(self):
        for remaining in (0, -1):
            with self.subTest(remaining=remaining), patch.object(
                self.client, "resolve_instance", return_value=SOURCE
            ), patch.object(self.client, "get_instance_left_copy", return_value={
                "data": {"clone_count_left": remaining}
            }), patch.object(self.client, "_request") as request:
                with self.assertRaisesRegex(RuntimeError, "clone_count_left"):
                    self.client.clone_instance_by_selector()
                request.assert_not_called()

    def test_clone_unknown_quota_does_not_reject(self):
        for quota in ({}, {"data": None}, {"data": {}}, {"data": 3},
                      {"data": {"data_copy_count_left": 0}}, {"data": {"clone_count_left": None}}):
            with self.subTest(quota=quota), patch.object(
                self.client, "resolve_instance", return_value=SOURCE
            ), patch.object(self.client, "get_instance_left_copy", return_value=quota), patch.object(
                self.client, "_request", return_value=SUCCESS
            ) as request:
                self.assertEqual(self.client.clone_instance_by_selector(), SUCCESS)
                self.assertEqual(request.call_args.args[0], "PATCH")

    def test_clone_resolution_failure_prevents_quota_and_patch(self):
        with patch.object(self.client, "resolve_instance", side_effect=RuntimeError("ambiguous")), patch.object(
            self.client, "get_instance_left_copy"
        ) as quota, patch.object(self.client, "_request") as request:
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                self.client.clone_instance_by_selector()
            quota.assert_not_called()
            request.assert_not_called()

    def test_release_json_endpoint_and_payload(self):
        with patch("gpufree.client.urllib.request.urlopen", return_value=response(SUCCESS)) as http:
            self.assertEqual(self.client.release_instance(42, SOURCE["webide_instance_uuid"]), SUCCESS)
        http.assert_called_once()
        self.assert_json_request(http.call_args.args[0], "DELETE", "/api/v1/inferring-api/webide/", {
            "instance_id": 42, "instance_uuid": SOURCE["webide_instance_uuid"],
        })

    def test_release_by_selector_uses_resolved_identifiers(self):
        for selector in (None, "worker", "42", SOURCE["webide_instance_uuid"]):
            with self.subTest(selector=selector), patch.object(
                self.client, "resolve_instance", return_value=SOURCE
            ) as resolve, patch.object(self.client, "release_instance", return_value=SUCCESS) as release:
                self.assertIs(self.client.release_instance_by_selector(selector), SUCCESS)
                resolve.assert_called_once_with(selector)
                release.assert_called_once_with(42, SOURCE["webide_instance_uuid"])

    def test_release_resolution_failure_prevents_delete(self):
        with patch.object(self.client, "resolve_instance", side_effect=RuntimeError("ambiguous")), patch.object(
            self.client, "_request"
        ) as request:
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                self.client.release_instance_by_selector()
            request.assert_not_called()

    def test_all_five_controls_share_success_and_error_semantics(self):
        controls = [lambda: self.client.start_instance(42, "uuid"),
                    lambda: self.client.stop_instance(42, "uuid"),
                    lambda: self.client.create_instance(**CREATE_ARGS),
                    lambda: self.client.clone_instance_by_selector(),
                    lambda: self.client.release_instance(42, "uuid")]
        with patch.object(self.client, "resolve_instance", return_value=SOURCE), patch.object(
            self.client, "get_instance_left_copy", return_value={"data": {"clone_count_left": 4}}
        ):
            for index, control in enumerate(controls):
                for code in (200, "200"):
                    payload = {"code": code, "data": {"result": "unchanged"}}
                    with self.subTest(control=index, code=code), patch.object(
                        self.client, "_request", return_value=payload
                    ):
                        self.assertIs(control(), payload)
                for code in (53522, "53522", 42408, "42408", 999, None):
                    with self.subTest(control=index, code=code), patch.object(
                        self.client, "_request", return_value={"code": code, "msg": "original"}
                    ):
                        with self.assertRaises(PlatformError) as raised:
                            control()
                        self.assertEqual(raised.exception.code, code)
                        self.assertEqual(raised.exception.msg, "original")
                        self.assertIn("original", str(raised.exception))
                        if str(code) == "53522":
                            self.assertIn("源实例需处于关机状态；克隆前请先 stop 源实例", str(raised.exception))
                        if str(code) == "42408":
                            self.assertIn("余额不足，请先充值", str(raised.exception))


class LifecycleCLITests(LifecycleTestCase):
    def test_create_defaults_dispatch_and_filtered_json(self):
        payload = {"code": 200, "data": {"instance_uuid": "new", "ssh_password": "secret",
                   "access_token": "secret", "nested": [{"refresh_token": "secret", "ok": 1}]}}
        with patch.object(self.client, "create_instance", return_value=payload) as create:
            code, out, err = self.cli(["create", "--sku", "test-sku", "--dc", "7", "--image", "19"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"code": 200, "data": {"instance_uuid": "new", "nested": [{"ok": 1}]}})
        create.assert_called_once_with(**CREATE_ARGS, req_amount=1, start_model="gpu", charge_type=0,
                                       expand_data_disk_gb=0, duration_type="", duration_count=1)

    def test_create_all_cli_options(self):
        with patch.object(self.client, "create_instance", return_value=SUCCESS) as create:
            code, out, err = self.cli(["create", "--sku", "test-sku", "--dc", "7", "--image", "19",
                                       "--count", "4", "--mode", "nogpu", "--charge-type", "1",
                                       "--expand-disk-gb", "3", "--duration-type", "month", "--duration-count", "2"])
        self.assertEqual((code, err, json.loads(out)), (0, "", SUCCESS))
        create.assert_called_once_with(**CREATE_ARGS, req_amount=4, start_model="nogpu", charge_type=1,
                                       expand_data_disk_gb=3, duration_type="month", duration_count=2)

    def test_create_required_options_fail_before_client_construction(self):
        for missing in ("--sku", "--dc", "--image"):
            args = ["create"]
            for flag, value in (("--sku", "test-sku"), ("--dc", "7"), ("--image", "19")):
                if flag != missing:
                    args.extend([flag, value])
            with self.subTest(missing=missing), patch("sys.argv", ["gpufree", *args]), patch(
                "gpufree.cli.GPUFeeClient"
            ) as construct, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main()
                self.assertEqual(raised.exception.code, 2)
                construct.assert_not_called()

    def test_create_validation_exits_one_without_request(self):
        for flags in (["--count", "0"], ["--charge-type", "2"]):
            with self.subTest(flags=flags), patch.object(self.client, "_request") as request:
                code, out, err = self.cli(["create", "--sku", "test-sku", "--dc", "7", "--image", "19", *flags])
            self.assertEqual((code, out), (1, ""))
            self.assertTrue(err.startswith("Error: "))
            request.assert_not_called()

    def test_clone_default_and_override_dispatch(self):
        for argv, selector, options in (
            (["clone"], None, {"nickname": None, "clone_data": False, "expand_data_disk_gb": 0}),
            (["clone", "worker", "--name", "copy", "--with-data", "--expand-disk-gb", "6"],
             "worker", {"nickname": "copy", "clone_data": True, "expand_data_disk_gb": 6}),
        ):
            with self.subTest(argv=argv), patch.object(self.client, "clone_instance_by_selector", return_value=SUCCESS) as clone:
                code, out, err = self.cli(argv)
            self.assertEqual((code, err, json.loads(out)), (0, "", SUCCESS))
            clone.assert_called_once_with(selector, **options)

    def test_clone_prechecks_exit_one_without_patch(self):
        for source, quota, message in ((dict(SOURCE, status=3), 4, "关机"), (SOURCE, 0, "clone_count_left")):
            with self.subTest(message=message), patch.object(
                self.client, "resolve_instance", return_value=source
            ), patch.object(self.client, "get_instance_left_copy", return_value={"data": {"clone_count_left": quota}}), patch.object(
                self.client, "_request"
            ) as request:
                code, out, err = self.cli(["clone", "worker"])
            self.assertEqual((code, out), (1, ""))
            self.assertIn(message, err)
            request.assert_not_called()

    def test_release_valid_confirmation_resolves_once_and_deletes_confirmed_target(self):
        for confirmation in ("abcde", "ABCDE", SOURCE["webide_instance_uuid"], SOURCE["webide_instance_uuid"].upper()):
            with self.subTest(confirmation=confirmation), patch.object(
                self.client, "resolve_instance", return_value=SOURCE
            ) as resolve, patch.object(self.client, "_request", return_value=SUCCESS) as request:
                code, out, err = self.cli(["release", "worker", "--confirm", confirmation])
            self.assertEqual((code, err, json.loads(out)), (0, "", SUCCESS))
            resolve.assert_called_once_with("worker")
            request.assert_called_once_with("DELETE", "/api/v1/inferring-api/webide/", {
                "instance_id": 42, "instance_uuid": SOURCE["webide_instance_uuid"],
            }, json_body=True)

    def test_release_sole_instance(self):
        with patch.object(self.client, "resolve_instance", return_value=SOURCE) as resolve, patch.object(
            self.client, "release_instance", return_value=SUCCESS
        ):
            code, out, err = self.cli(["release", "--confirm", "abcde"])
        self.assertEqual((code, err, json.loads(out)), (0, "", SUCCESS))
        resolve.assert_called_once_with(None)

    def test_release_missing_or_wrong_confirmation_prevents_delete(self):
        for confirmation in (None, "", "wrong", "abcd", "abcdef", "abcde-wrong-suffix"):
            flags = [] if confirmation is None else ["--confirm", confirmation]
            with self.subTest(confirmation=confirmation), patch.object(
                self.client, "resolve_instance", return_value=SOURCE
            ) as resolve, patch.object(self.client, "_request") as request, patch.object(
                self.client, "release_instance"
            ) as release:
                code, out, err = self.cli(["release", "worker", *flags])
            self.assertEqual((code, out), (1, ""))
            self.assertIn("--confirm", err)
            request.assert_not_called()
            release.assert_not_called()
            if not confirmation:
                resolve.assert_not_called()

    def test_release_ambiguous_target_prevents_delete(self):
        with patch.object(self.client, "resolve_instance", side_effect=RuntimeError("ambiguous")), patch.object(
            self.client, "_request"
        ) as request:
            code, out, err = self.cli(["release", "--confirm", "abcde"])
        self.assertEqual((code, out, err), (1, "", "Error: ambiguous\n"))
        request.assert_not_called()

    def test_all_commands_runtime_and_value_errors_exit_one(self):
        for argv, method in (
            (["create", "--sku", "test-sku", "--dc", "7", "--image", "19"], "create_instance"),
            (["clone"], "clone_instance_by_selector"),
            (["release", "--confirm", "abcde"], "release_instance"),
        ):
            for error in (RuntimeError("failure"), ValueError("invalid"), PlatformError(42408, "balance")):
                with self.subTest(argv=argv, error=error), patch.object(
                    self.client, "resolve_instance", return_value=SOURCE
                ), patch.object(self.client, method, side_effect=error):
                    code, out, err = self.cli(argv)
                self.assertEqual((code, out, err), (1, "", f"Error: {error}\n"))

    def test_clone_and_release_success_filter_response(self):
        payload = {"code": 200, "data": {"ssh_password": "secret", "id_token": "secret", "ok": 1}}
        for argv, method in ((["clone"], "clone_instance_by_selector"),
                             (["release", "--confirm", "abcde"], "release_instance")):
            with self.subTest(argv=argv), patch.object(
                self.client, "resolve_instance", return_value=SOURCE
            ), patch.object(self.client, method, return_value=payload):
                code, out, err = self.cli(argv)
            self.assertEqual((code, err, json.loads(out)), (0, "", {"code": 200, "data": {"ok": 1}}))


if __name__ == "__main__":
    unittest.main()
