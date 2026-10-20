from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mikachan.bot import App, download_prompt_markup
from mikachan.follow import (FollowService, creator_from_post, profile_target,
                               resolve_creator_from_post)
from mikachan.jobs import Worker
from mikachan.store import Store


def labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


@pytest.mark.parametrize("url", [
    "https://www.instagram.com/p/ABC123/",
    "https://www.tiktok.com/@alice/video/123",
    "https://www.youtube.com/shorts/abcdefghijk",
    "https://x.com/alice/status/123",
])
def test_profile_button_before_and_after_download_without_creator_metadata(url):
    assert "👤 Profile" in labels(download_prompt_markup("token", url))
    assert "👤 Profile" in labels(Worker._buttons("job", url, False, False, False))


def test_story_links_reveal_public_creator_without_lookup():
    assert creator_from_post("https://www.instagram.com/stories/alice/123/").handle == "alice"
    assert creator_from_post("https://www.tiktok.com/@alice/stories").handle == "alice"


@pytest.mark.asyncio
async def test_resolve_instagram_and_youtube_creator_from_public_metadata(monkeypatch):
    monkeypatch.setattr("mikachan.follow.validate_dns", lambda _url: None)
    async def local_thread(fn, *args):
        return fn(*args)
    monkeypatch.setattr("mikachan.follow.asyncio.to_thread", local_thread)
    async def extract(command, _timeout):
        if "gallery-dl" in command:
            return [[2, {"username": "alice"}]]
        return {"channel_url": "https://www.youtube.com/@alice"}
    monkeypatch.setattr("mikachan.follow._extract", extract)
    instagram = await resolve_creator_from_post("https://www.instagram.com/p/ABC123/")
    youtube = await resolve_creator_from_post("https://www.youtube.com/shorts/abcdefghijk")
    assert instagram == profile_target("https://www.instagram.com/alice/")
    assert youtube == profile_target("https://www.youtube.com/@alice")


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kwargs):
        message_id = 200 + len(self.sent)
        self.sent.append((chat, text, kwargs, message_id))
        return SimpleNamespace(message_id=message_id)


def query(data, message_id):
    async def answer(_text=None):
        return None
    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=42),
        message=SimpleNamespace(chat=SimpleNamespace(id=42, type="private"),
                                message_id=message_id), answer=answer)


def make_app(tmp_path):
    app = App.__new__(App)
    app.store = Store(tmp_path / "bot.sqlite")
    app.bot = FakeBot()
    app.follow = FollowService(app.store, app.bot)
    app.authorized = lambda _user, _chat, _type: True
    return app


@pytest.mark.asyncio
async def test_predownload_profile_callback_opens_panel_and_follow(monkeypatch, tmp_path):
    app = make_app(tmp_path)
    app.instagram_relay = SimpleNamespace(show_profile=AsyncMock())
    url = "https://www.instagram.com/p/ABC123/"
    cid = app.store.confirm_create(42, 42, 100, 0, url, 900)
    assert app.store.claim_prompt(cid)
    app.store.set_prompt(cid, 101)
    async def resolve(_url, _meta=None):
        return profile_target("https://www.instagram.com/alice/")
    monkeypatch.setattr("mikachan.bot.resolve_creator_from_post", resolve)
    await app.callback(query(f"c:{cid}:p", 101))
    app.instagram_relay.show_profile.assert_awaited_once_with(42, 42, "alice")
    assert app.bot.sent == []
    state = app.store.db.execute("SELECT state FROM confirmations WHERE id=?", (cid,)).fetchone()
    assert state["state"] == "PENDING"


@pytest.mark.asyncio
async def test_postdownload_profile_callback_resolves_missing_metadata(monkeypatch, tmp_path):
    app = make_app(tmp_path)
    url = "https://www.youtube.com/shorts/abcdefghijk"
    jid = app.store.create_job(42, 42, url)
    async def resolve(_url, _meta=None):
        return profile_target("https://www.youtube.com/@alice")
    monkeypatch.setattr("mikachan.bot.resolve_creator_from_post", resolve)
    await app.callback(query(f"j:{jid}:profile", 101))
    assert "👀 Follow" in labels(app.bot.sent[0][2]["reply_markup"])
