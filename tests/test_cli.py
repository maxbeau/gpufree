import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from gpufree import __version__
from gpufree.cli import main


class CLITests(unittest.TestCase):
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
