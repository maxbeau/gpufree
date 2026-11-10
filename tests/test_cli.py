import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from gpufree import __version__
from gpufree.cli import main


class CLITests(unittest.TestCase):
    def test_manual_control_dispatch(self):
        cases = [
            (["start"], "start", (None,), {"start_mode": "gpu", "wait": False, "wait_timeout": 120}),
            (["start", "worker", "--mode", "nogpu", "--wait", "--wait-timeout", "30"],
             "start", ("worker",), {"start_mode": "nogpu", "wait": True, "wait_timeout": 30}),
            (["stop", "12", "--wait"], "stop", ("12",), {"wait": True, "wait_timeout": 120}),
            (["stop"], "stop", (None,), {"wait": False, "wait_timeout": 120}),
        ]
        for argv, action, args, kwargs in cases:
            client = MagicMock()
            method = getattr(client, action + "_instance_by_selector")
            method.return_value = {"code": 200}
            output = io.StringIO()
            with self.subTest(argv=argv), patch("sys.argv", ["gpufree", *argv]), patch(
                "gpufree.cli.GPUFeeClient", return_value=client
            ), redirect_stdout(output):
                self.assertEqual(main(), 0)
            method.assert_called_once_with(*args, **kwargs)
            self.assertIn(action + " requested for", output.getvalue())
            self.assertIn('"code": 200', output.getvalue())

    def test_manual_control_errors(self):
        for action in ("start", "stop"):
            client = MagicMock()
            getattr(client, action + "_instance_by_selector").side_effect = RuntimeError("ambiguous instance")
            output = io.StringIO()
            with self.subTest(action=action), patch("sys.argv", ["gpufree", action]), patch(
                "gpufree.cli.GPUFeeClient", return_value=client
            ), redirect_stderr(output):
                self.assertEqual(main(), 1)
            self.assertEqual(output.getvalue(), "Error: ambiguous instance\n")

    def test_version(self):
        output = io.StringIO()
        with patch("sys.argv", ["gpufree", "--version"]), redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"gpufree {__version__}")

    def test_instance_left_copy_command(self):
        client = MagicMock()
        client.get_instance_left_copy.return_value = {"data": 3}
        output = io.StringIO()
        with patch("sys.argv", ["gpufree", "instance-left-copy"]), patch(
            "gpufree.cli.GPUFeeClient", return_value=client
        ), redirect_stdout(output):
            exit_code = main()
        self.assertEqual(exit_code, 0)
        self.assertEqual(client.get_instance_left_copy.call_count, 1)
        self.assertEqual(output.getvalue().strip(), '{\n  "data": 3\n}')

    def test_client_id_uses_public_source_property(self):
        client = MagicMock(client_id="client-id", client_id_source="stored JWT")
        output = io.StringIO()
        with patch("sys.argv", ["gpufree", "client-id"]), patch(
            "gpufree.cli.GPUFeeClient", return_value=client
        ), redirect_stdout(output):
            exit_code = main()
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            output.getvalue().splitlines(),
            ["client_id: client-id", "source:     stored JWT"],
        )


if __name__ == "__main__":
    unittest.main()
