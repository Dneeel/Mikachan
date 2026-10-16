import tempfile
import unittest
from pathlib import Path
from pydantic import ValidationError
from mika_nexus.config import Settings


class ConfigTests(unittest.TestCase):
    def test_fail_closed(self):
        values = dict(bot_token="123:fake", owner_telegram_id=1, allowed_telegram_ids="1")
        self.assertEqual(Settings(**values).allowlist, {1})
        for ids in ("", "2", "1,2,3,4,5", "1,abc", "1,1"):
            with self.subTest(ids=ids), self.assertRaises(ValidationError):
                Settings(**{**values, "allowed_telegram_ids": ids})
        with self.assertRaises(ValidationError):
            Settings(**{**values, "bot_api_mode": "local", "local_bot_api_url": "http://example.com:8080", "telegram_api_id": 123, "telegram_api_hash": "hash"})


if __name__ == "__main__":
    unittest.main()
