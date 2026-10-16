"""Runs when aiogram dependencies are installed; no real Bot API calls."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from mika_nexus.bot import App
except ImportError:
    App = None

from mika_nexus.config import Settings
from mika_nexus.store import Store


class FakePrompt:
    def __init__(self, chat, message_id):
        self.chat = chat
        self.message_id = message_id
        self.deleted = False
        self.edited = None

    async def delete(self):
        self.deleted = True

    async def edit_text(self, text, **kwargs):
        self.edited = text

    async def edit_reply_markup(self, **kwargs):
        self.edited = "markup removed"


class FakeMessage:
    def __init__(self, text, chat, message_id, actor=1):
        self.text, self.chat, self.message_id = text, chat, message_id
        self.from_user = SimpleNamespace(id=actor)
        self.replies = []

    async def reply(self, text, **kwargs):
        prompt = FakePrompt(self.chat, 100 + len(self.replies) + self.message_id)
        prompt.text = text
        prompt.keyboard = kwargs.get("reply_markup")
        self.replies.append(prompt)
        return prompt


class FakeCallback:
    def __init__(self, data, message, actor=1):
        self.data, self.message = data, message
        self.from_user = SimpleNamespace(id=actor)
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True


@unittest.skipIf(App is None, "aiogram is not installed")
class TelegramFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_consent_and_silent_text(self):
        with tempfile.TemporaryDirectory() as d:
            app = object.__new__(App)
            app.settings = Settings(bot_token="123:fake", owner_telegram_id=1, allowed_telegram_ids="1", data_dir=Path(d))
            app.store = Store(Path(d) / "state.sqlite")
            app.store.seed_access({1})
            chat = SimpleNamespace(id=1, type="private")
            plain = FakeMessage("Radiohead", chat, 1)
            await app.message(plain)
            self.assertEqual(plain.replies, [])
            link = FakeMessage("https://example.com/file.mp4", chat, 2)
            await app.message(link)
            self.assertEqual(len(link.replies), 1)
            prompt = link.replies[0]
            buttons = prompt.keyboard.inline_keyboard[0]
            self.assertEqual([b.text for b in buttons], ["✅ Yes", "❌ No"])
            self.assertEqual(app.store.jobs(1), [])
            denied = FakeCallback(buttons[0].callback_data, prompt, actor=2)
            await app.callback(denied)
            self.assertEqual(app.store.jobs(1), [])
            no = FakeCallback(buttons[1].callback_data, prompt)
            await app.callback(no)
            self.assertTrue(prompt.deleted)
            self.assertEqual(app.store.jobs(1), [])
            link2 = FakeMessage("https://example.com/other.mp4", chat, 3)
            await app.message(link2)
            yes_button = link2.replies[0].keyboard.inline_keyboard[0][0]
            yes = FakeCallback(yes_button.callback_data, link2.replies[0])
            await app.callback(yes)
            await app.callback(yes)
            self.assertEqual(len(app.store.jobs(1)), 1)


if __name__ == "__main__":
    unittest.main()
