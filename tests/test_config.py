import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from pydantic import ValidationError
from mikachan.config import ProviderSettings, Settings


class ConfigTests(unittest.TestCase):
    def test_optional_spotify_credentials_are_read_from_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / ".env"
            env_file.write_text("SPOTIFY_CLIENT_ID=example-id\nSPOTIFY_CLIENT_SECRET=example-secret\n")
            with patch.dict("os.environ", {}, clear=True):
                providers = ProviderSettings(_env_file=env_file)
        self.assertEqual(providers.spotify_client_id, "example-id")
        self.assertEqual(providers.spotify_client_secret, "example-secret")

    def test_fail_closed(self):
        values = dict(bot_token="123:fake", owner_telegram_id=1, allowed_telegram_ids="1")
        self.assertEqual(Settings(**values).allowlist, {1})
        for ids in ("", "2", "1,2,3,4,5", "1,abc", "1,1"):
            with self.subTest(ids=ids), self.assertRaises(ValidationError):
                Settings(**{**values, "allowed_telegram_ids": ids})
        with self.assertRaises(ValidationError):
            Settings(
                **{
                    **values,
                    "bot_api_mode": "local",
                    "local_bot_api_url": "http://example.com:8080",
                    "telegram_api_id": 123,
                    "telegram_api_hash": "hash",
                }
            )


if __name__ == "__main__":
    unittest.main()
