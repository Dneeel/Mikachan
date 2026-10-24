"""TikTok discovery validates provider data and preserves button ownership."""

import pytest

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
        message = type("Message", (), {"message_id": 1,
            "chat": type("Chat", (), {"id": -100})()})()

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
