"""Runs when aiogram dependencies are installed; no real Bot API calls."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from mika_nexus.bot import App, public_media_result
    from mika_nexus.jobs import extractor_error
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


class StaleCallback(FakeCallback):
    async def answer(self, *args, **kwargs):
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import AnswerCallbackQuery

        raise TelegramBadRequest(
            method=AnswerCallbackQuery(callback_query_id="expired"),
            message="query is too old and response timeout expired or query ID is invalid",
        )


class FakeBot:
    def __init__(self):
        self.edits = []
        self.sent = []

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def send_message(self, chat_id, text, **kwargs):
        prompt = FakePrompt(SimpleNamespace(id=chat_id, type="private"), 301)
        prompt.text = text
        prompt.keyboard = kwargs.get("reply_markup")
        self.sent.append(prompt)
        return prompt


@unittest.skipIf(App is None, "aiogram is not installed")
class TelegramFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_search_explains_next_step(self):
        with tempfile.TemporaryDirectory() as d:
            app = object.__new__(App)
            app.settings = Settings(
                bot_token="123:fake",
                owner_telegram_id=1,
                allowed_telegram_ids="1",
                data_dir=Path(d),
            )
            app.store = Store(Path(d) / "state.sqlite")
            app.store.seed_access({1})
            sid = app.store.create_search(1, 1, "sample", {"track"}, {"soundcloud"})
            app.store.set_search_message(sid, 20)
            app.store.db.execute("UPDATE search_sessions SET expires=0 WHERE id=?", (sid,))
            message = FakePrompt(SimpleNamespace(id=1, type="private"), 20)
            await app.callback(StaleCallback(f"s:{sid}:b", message))
            self.assertIn("Search expired", message.edited)

    async def test_extractor_errors_are_specific_and_do_not_echo_links(self):
        cases = [
            (
                "https://x.com/a/status/1",
                b"No video could be found in this tweet",
                "no downloadable video",
            ),
            (
                "https://instagram.com/stories/a/1",
                b"Use --cookies for authentication",
                "requires login",
            ),
            (
                "https://instagram.com/p/abc",
                b"No video formats found!",
                "no publicly extractable video",
            ),
        ]
        for url, detail, expected in cases:
            result = extractor_error(url, detail + url.encode())
            self.assertIn(expected, result)
            self.assertNotIn(url, result)

    async def test_stale_search_callback_can_offer_public_media(self):
        with tempfile.TemporaryDirectory() as d:
            app = object.__new__(App)
            app.settings = Settings(
                bot_token="123:fake",
                owner_telegram_id=1,
                allowed_telegram_ids="1",
                data_dir=Path(d),
            )
            app.store = Store(Path(d) / "state.sqlite")
            app.store.seed_access({1})
            app.bot = FakeBot()
            app.searcher = SimpleNamespace(health={})
            sid = app.store.create_search(1, 1, "sample", {"track"}, {"soundcloud"})
            app.store.set_search_message(sid, 20)
            app.store.save_results(
                sid,
                0,
                [
                    {
                        "source": "soundcloud",
                        "kind": "track",
                        "title": "Public track",
                        "creator": "Artist",
                        "url": "https://soundcloud.com/artist/public-track",
                        "id": "track-1",
                    }
                ],
            )
            message = FakePrompt(SimpleNamespace(id=1, type="private"), 20)
            await app.callback(StaleCallback(f"s:{sid}:d:0:0", message))
            self.assertIn("Request public media", str(app.bot.edits[-1][1]["reply_markup"]))
            await app.callback(StaleCallback(f"s:{sid}:r:0:0", message))
            self.assertEqual(len(app.bot.sent), 1)
            self.assertEqual(app.store.jobs(1), [])
            self.assertFalse(public_media_result({"source": "spotify", "kind": "track"}))

    async def test_consent_and_silent_text(self):
        with tempfile.TemporaryDirectory() as d:
            app = object.__new__(App)
            app.settings = Settings(
                bot_token="123:fake",
                owner_telegram_id=1,
                allowed_telegram_ids="1",
                data_dir=Path(d),
            )
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
