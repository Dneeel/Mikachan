"""TikTok discovery validates provider data and preserves button ownership."""

from datetime import datetime, timezone
import json

import httpx
import pytest
from aiogram.types import Chat, Message

from mikachan.explore import Explore, _post, topic_from_arg


def test_topic_and_media_validation():
    assert topic_from_arg("#attackontitan") == "attackontitan"
    assert topic_from_arg("#MW4") == "MW4"
    with pytest.raises(ValueError):
        topic_from_arg("#bad/topic")
    assert _post({"video_id": "1234567890123456789", "author": {"unique_id": "alice"},
                  "play": "https://attacker.example/video.mp4"}) is None
    post = _post({"video_id": "1234567890123456789",
                  "author": {"unique_id": "alice"},
                  "images": ["https://p16.tiktokcdn-us.com/a.jpg"]})
    assert post and post["images"]


@pytest.mark.asyncio
async def test_topic_search_403_uses_bounded_feed_and_filters_topic(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/search"):
            return httpx.Response(403)
        return httpx.Response(200, content=json.dumps({"code": 0, "data": [
            {"id": "1234567890123456789", "author": {"unique_id": "alice"},
             "title": "#edit from today", "play": "https://tikwm.com/a.mp4"},
            {"id": "2234567890123456789", "author": {"unique_id": "bob"},
             "title": "unrelated", "play": "https://tikwm.com/b.mp4"}]}))
    original = httpx.AsyncClient
    monkeypatch.setattr("mikachan.explore.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    explore = Explore(object())
    _token, session = explore._new(7, 70, "edit")
    result = await explore._find(session)
    assert result["handle"] == "alice"
    assert seen.count("/api/feed/list") == 4


@pytest.mark.asyncio
async def test_exact_challenge_identity_returns_posts_without_caption_hashtag(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/challenge/search"):
            return httpx.Response(200, json={"code": 0, "data": {"challenge_list": [
                {"id": "1", "cha_name": "MW4unrelated"},
                {"id": "29106061", "cha_name": "mw4"}]}})
        assert request.url.params["challenge_id"] == "29106061"
        return httpx.Response(200, json={"code": 0, "data": {"videos": [
            {"video_id": "1234567890123456789", "author": {"unique_id": "alice"},
             "title": "No hashtag repeated in caption", "play": "https://tikwm.com/a.mp4"},
            {"video_id": "2234567890123456789", "author": {"unique_id": "bob"},
             "title": "Another post", "play": "https://tikwm.com/b.mp4"}]}})
    original = httpx.AsyncClient
    monkeypatch.setattr("mikachan.explore.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    explore = Explore(object())
    _token, session = explore._new(7, 70, "MW4")
    result = await explore._find(session)
    assert result["handle"] in {"alice", "bob"}
    assert seen == ["/api/challenge/search", "/api/challenge/posts"]
    session.seen.add(str(result["id"]))
    second = await explore._find(session)
    assert second["id"] != result["id"]
    assert seen == ["/api/challenge/search", "/api/challenge/posts"]


@pytest.mark.asyncio
async def test_topic_and_one_more_keep_subject_and_send_new_message(monkeypatch):
    class Bot:
        def __init__(self):
            self.sent = []

        async def send_video(self, chat_id, url, **kwargs):
            self.sent.append((chat_id, url, kwargs))
            return type("Sent", (), {"message_id": len(self.sent)})()

    bot = Bot()
    explore = Explore(bot)
    token, session = explore._new(42, -100, "MW4")
    posts = iter([{"id": "1234567890123456789", "handle": "alice", "images": [],
                   "video": "https://sf16.tiktokcdn-us.com/one.mp4", "title": "first"},
                  {"id": "2234567890123456789", "handle": "bob", "images": [],
                   "video": "https://sf16.tiktokcdn-us.com/two.mp4", "title": "second"}])

    async def find(current):
        assert current.topic == "MW4"
        return next(posts)

    monkeypatch.setattr(explore, "_find", find)
    await explore.send_next(token, session)
    assert session.message_id == 1

    class Query:
        data = f"ex:{token}:more"
        from_user = type("User", (), {"id": 42})()
        message = Message(message_id=1, date=datetime.now(timezone.utc),
                          chat=Chat(id=-100, type="supergroup"))

        async def answer(self, *_args):
            pass

    assert await explore.callback(Query())
    assert len(bot.sent) == 2 and session.message_id == 2
    assert "#MW4" in bot.sent[1][2]["caption"]
    assert bot.sent[0][2]["reply_markup"].inline_keyboard[0][0].callback_data == \
        f"ex:{token}:more"
    Query.from_user = type("User", (), {"id": 43})()
    assert await explore.callback(Query())
    assert len(bot.sent) == 2
