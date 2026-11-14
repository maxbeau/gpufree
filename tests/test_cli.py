import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from gpufree import __version__
from gpufree.cli import main


class CLITests(unittest.TestCase):
    def test_help_excludes_removed_flat_commands(self):
        output = io.StringIO()
        with patch("sys.argv", ["gpufree", "--help"]), redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 0)
        for command in ("balance", "instances", "start", "stop", "status", "refresh", "login", "products"):
            self.assertNotIn("{" + command + "}", output.getvalue())
        for command in ("instance", "billing", "catalog", "auth", "snapshot", "api"):
            self.assertIn(command, output.getvalue())

    def test_version(self):
        output = io.StringIO()
        with patch("sys.argv", ["gpufree", "--version"]), redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"gpufree {__version__}")


if __name__ == "__main__":
    unittest.main()
