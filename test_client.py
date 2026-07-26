import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gpufree.client import GPUFeeClient


def jwt(iat, exp):
    payload = base64.urlsafe_b64encode(
        json.dumps({"iat": iat, "exp": exp}).encode()
    ).rstrip(b"=").decode()
    return f"header.{payload}.signature"


class TokenRefreshTests(unittest.TestCase):
    def make_client(self, access_exp, id_exp):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "tokens.json"
        now = int(time.time())
        path.write_text(json.dumps({
            "access_token": jwt(now, access_exp),
            "id_token": jwt(now, id_exp),
            "refresh_token": "refresh",
        }))
        return directory, GPUFeeClient(path)

    def test_id_token_controls_session_status(self):
        now = int(time.time())
        directory, client = self.make_client(now + 604800, now - 1)
        self.addCleanup(directory.cleanup)
        status = client.check_expiry()
        self.assertEqual(status["status"], "refresh_required")
        self.assertEqual(status["tokens"]["access_token"]["status"], "valid")
        self.assertEqual(status["tokens"]["id_token"]["status"], "expired")

    def test_request_refreshes_before_expired_id_token_is_used(self):
        now = int(time.time())
        directory, client = self.make_client(now + 604800, now + 60)
        self.addCleanup(directory.cleanup)
        with patch.object(client, "refresh_token") as refresh, patch(
            "urllib.request.urlopen"
        ) as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"
            client._get("/test")
        refresh.assert_called_once_with()

    def test_request_does_not_refresh_healthy_session(self):
        now = int(time.time())
        directory, client = self.make_client(now + 604800, now + 3600)
        self.addCleanup(directory.cleanup)
        with patch.object(client, "refresh_token") as refresh, patch(
            "urllib.request.urlopen"
        ) as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"
            client._get("/test")
        refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
