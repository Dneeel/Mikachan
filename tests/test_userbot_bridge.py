"""Offline checks for opt-in MTProto routing and input boundaries."""

import sqlite3
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mikachan.access import AccessRegistry
from mikachan.bot import App
from mikachan.store import Store
from mikachan.userbot import (UserbotBridge, extract_download_url,
                               extract_service_urls, is_currency_query)


def test_file_link_parser_accepts_public_https_only():
    answer = ("Your Link Generated!\nDownload: "
              "https://files.example.org/stream/317555?hash=378ba2&d=true\n"
              "All links expire after 24 hours")
    assert extract_download_url(answer) == \
        "https://files.example.org/stream/317555?hash=378ba2&d=true"
    for bad in ("Download: http://127.0.0.1/file", "Download: http://localhost/file",
                "No link yet", "Download: javascript:alert(1)"):
        with pytest.raises(ValueError):
            extract_download_url(bad)


def test_internal_link_parser_collects_public_https_from_text_and_buttons():
    message = SimpleNamespace(raw_text="Link: https://files.example/one\n"
        "Private: http://127.0.0.1/secret", buttons=[[
            SimpleNamespace(url="https://files.example/two"),
            SimpleNamespace(url="https://t.me/provider")]])
    assert extract_service_urls(message) == [
        "https://files.example/one", "https://files.example/two"]


@pytest.mark.asyncio
async def test_url_choice_is_bound_to_requester_and_uses_internal_provider(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    store.db.execute("CREATE TABLE url_links(token TEXT PRIMARY KEY,user_id INTEGER,"
                     "chat_id INTEGER,message_id INTEGER,url TEXT,expires REAL)")
    original = SimpleNamespace(message_id=7)
    prompt = SimpleNamespace(chat=SimpleNamespace(id=1, type="private"), message_id=9,
        edit_text=AsyncMock(), delete=AsyncMock())
    q = SimpleNamespace(from_user=SimpleNamespace(id=2), message=prompt,
        data="urlchoice:secret:internal", answer=AsyncMock())
    app = App.__new__(App)
    app.store = store
    app.authorized = lambda *_args: True
    app._url_choices = {"secret": (1, 1, 9, original, __import__("time").time()+60)}
    app.userbot = SimpleNamespace(file_to_links=AsyncMock(return_value=[
        "https://files.example/one", "https://files.example/two"]))
    app.bot = SimpleNamespace(send_document=AsyncMock())
    await app.callback(q)
    app.userbot.file_to_links.assert_not_awaited()
    q.from_user.id = 1
    await app.callback(q)
    app.userbot.file_to_links.assert_awaited_once_with(app.bot, 1, 7, original,
                                                        internal=True)
    assert "files.example/two" in prompt.edit_text.await_args.args[0]


@pytest.mark.parametrize("query", ["usd", "101 usd", "2300 jpy to usd", "19 eur to usdt",
                                   "دلار به ین", "120000 دینار عراق به یورو", "12 سکه به دلار"])
def test_currency_query_matches_explicit_requests(query):
    assert is_currency_query(query)


@pytest.mark.parametrize("query", ["I have usd", "https://example.com", "btc to something",
                                   "send all messages", "usd\npassword"])
def test_currency_query_ignores_unrelated_text(query):
    assert not is_currency_query(query)


def test_owner_controls_registered_private_chat():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript("CREATE TABLE access(user_id INTEGER PRIMARY KEY);"
                     "CREATE TABLE allowed_groups(chat_id INTEGER PRIMARY KEY);")
    registry = AccessRegistry(db, 1)
    registry.register(1, 2, "Second account")
    assert not registry.can_access(2, 2, "private")
    registry.set_private(1, 2, True)
    assert registry.can_access(2, 2, "private")
    assert not registry.can_access(2, 3, "private")
    registry.set_private(1, 2, False)
    assert not registry.can_access(2, 2, "private")


@pytest.mark.asyncio
async def test_url_uses_bot_download_when_no_staging_chat(tmp_path):
    peer = SimpleNamespace(id=77, bot=True)

    class Conversation:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def send_file(self, path):
            assert path.endswith(".mp4")
            return SimpleNamespace(id=100)

        async def get_response(self):
            return SimpleNamespace(sender_id=77, id=101,
                                   raw_text="Download: https://files.example/stream/1")

    client = SimpleNamespace(is_connected=lambda: True,
                             is_user_authorized=AsyncMock(return_value=True),
                             get_entity=AsyncMock(return_value=peer),
                             conversation=lambda *_args, **_kwargs: Conversation())
    bridge = UserbotBridge.__new__(UserbotBridge)
    bridge.client, bridge.staging_chat_id, bridge.lock = client, 0, asyncio.Lock()
    bridge.last_request, bridge.cooldown_until, bridge.file_peer = 0.0, 0.0, None

    async def download(_media, *, destination, timeout):
        destination.write_bytes(b"video")

    bot = SimpleNamespace(download=AsyncMock(side_effect=download))
    empty = {name: None for name in ("document", "audio", "animation", "voice",
                                     "video_note", "photo", "sticker")}
    message = SimpleNamespace(**empty, video=SimpleNamespace(file_size=5, file_name="a.mp4"))
    assert await bridge.file_to_link(bot, 123, 456, message) == \
        "https://files.example/stream/1"
    bot.download.assert_awaited_once()


@pytest.mark.asyncio
async def test_url_rejects_large_bot_file_without_staging_chat():
    bridge = UserbotBridge.__new__(UserbotBridge)
    bridge.client = SimpleNamespace(is_connected=lambda: True,
                                    is_user_authorized=AsyncMock(return_value=True),
                                    get_entity=AsyncMock(return_value=SimpleNamespace(id=77, bot=True)))
    bridge.staging_chat_id, bridge.lock = 0, asyncio.Lock()
    bridge.last_request, bridge.cooldown_until, bridge.file_peer = 0.0, 0.0, None
    empty = {name: None for name in ("document", "audio", "animation", "voice",
                                     "video_note", "photo", "sticker")}
    message = SimpleNamespace(**empty, video=SimpleNamespace(file_size=21_000_000))
    with pytest.raises(ValueError, match="USERBOT_STAGING_CHAT_ID"):
        await bridge.file_to_link(SimpleNamespace(download=AsyncMock()), 123, 456, message)


@pytest.mark.asyncio
async def test_pornhub_relay_accepts_only_video_from_verified_provider():
    peer = SimpleNamespace(id=77, bot=True, username="PHDownlBot")
    sent = SimpleNamespace(id=101)
    unrelated = SimpleNamespace(id=102, sender_id=78, video=object(), media=object())
    result = SimpleNamespace(id=103, sender_id=77, video=object(), media=object())
    bridge = UserbotBridge.__new__(UserbotBridge)
    bridge.pornhub_lock = asyncio.Lock()
    bridge.pornhub_peer = None
    bridge._ready = AsyncMock()
    bridge.client = SimpleNamespace(get_entity=AsyncMock(return_value=peer),
        send_message=AsyncMock(return_value=sent),
        get_messages=AsyncMock(return_value=[unrelated, result]))
    url = "https://www.pornhub.com/view_video.php?viewkey=6ac5dd5aca7cb"
    assert await bridge.pornhub_video(url) is result
    bridge.client.send_message.assert_awaited_once_with(peer, url)
    with pytest.raises(ValueError, match="Pornhub"):
        await bridge.pornhub_video("https://example.com/video")
