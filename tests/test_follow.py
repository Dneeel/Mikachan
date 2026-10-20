import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mikachan.follow import (FollowService, FollowStore, _post_timestamp, command_target,
                               creator_from_post, profile_target, public_profile,
                               _gallery_items, public_items)
from mikachan.store import Store


def test_target_parser_rejects_posts_and_untrusted_hosts():
    assert profile_target("https://www.instagram.com/alice/?utm=1").url == (
        "https://www.instagram.com/alice/")
    assert command_target("twitter @alice").url == "https://x.com/alice"
    assert command_target("youtube alice").platform == "youtube"
    assert profile_target("https://www.youtube.com/@alice/shorts").url == "https://www.youtube.com/@alice"
    assert profile_target("https://www.instagram.com/p/shortcode/") is None
    assert profile_target("https://notinstagram.com/alice") is None
    assert creator_from_post("https://x.com/alice/status/1").handle == "alice"
    assert creator_from_post("https://www.instagram.com/p/code/", {"username": "alice"}).handle == "alice"


def test_session_binding_and_destination(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    follows = FollowStore(store.db)
    target = command_target("tiktok alice")
    token = follows.session(42, -100, target)
    follows.bind(token, 7)
    assert follows.get_session(token, 43, -100, 7) is None
    assert follows.get_session(token, 42, -101, 7) is None
    assert follows.get_session(token, 42, -100, 8) is None
    session = follows.get_session(token, 42, -100, 7)
    assert session is not None
    assert session["destination"] == "dm"
    follows.db.execute("UPDATE follow_sessions SET destination='here' WHERE id=?", (token,))
    session = follows.get_session(token, 42, -100, 7)
    follow_id = follows.save(session)
    row = follows.db.execute("SELECT * FROM follows WHERE id=?", (follow_id,)).fetchone()
    assert row["chat_id"] == -100
    assert set(json.loads(row["features"])) == {"posts", "stories", "live", "bio", "name", "avatar"}
    assert follows.remove(follow_id, 43) is False
    assert follows.remove(follow_id, 42) is True


def test_gallery_accepts_text_tweets_and_rejects_embedded_errors():
    target = command_target("twitter alice")
    data = [[2, {"tweet_id": 123, "content": "text only"}]]
    assert _gallery_items(data, target, "posts") == [
        ("tweets", "https://x.com/alice/status/123", "text only")]
    with pytest.raises(ValueError):
        _gallery_items([[-1, {"error": "HttpError"}]], target, "posts")


@pytest.mark.asyncio
async def test_tiktok_timeline_uses_second_public_extractor(monkeypatch):
    calls = []
    async def extract(command, _timeout):
        calls.append(command[0])
        if command[0] == "gallery-dl":
            raise ValueError("public source unavailable")
        return {"entries": [{"id": "1234567890123456789", "title": "new post"}]}
    monkeypatch.setattr("mikachan.follow.validate_dns", lambda _url: None)
    monkeypatch.setattr("mikachan.follow.public_statuses", AsyncMock(
        side_effect=ValueError("source unavailable")))
    monkeypatch.setattr("mikachan.follow._extract", extract)
    items = await public_items(command_target("tiktok alice"))
    assert calls == ["gallery-dl", "yt-dlp"]
    assert items == [("posts", "https://www.tiktok.com/@alice/video/1234567890123456789",
                      "new post")]


@pytest.mark.asyncio
async def test_story_source_failure_does_not_send_account_alert(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    class Bot:
        messages = []
        async def send_message(self, chat, text, **kwargs):
            self.messages.append(text)
    service = FollowService(store, Bot())
    row = {"id": "follow", "chat_id": 42, "handle": "alice"}
    for _ in range(4):
        await service._source_health(row, "stories", False)
    assert service.bot.messages == []


@pytest.mark.asyncio
async def test_first_poll_baselines_each_category_and_new_post_notifies(monkeypatch, tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.seed_access({42})
    class Bot:
        def __init__(self):
            self.messages = []
        async def send_message(self, chat, text, **kwargs):
            self.messages.append((chat, text, kwargs))
    bot = Bot()
    service = FollowService(store, bot)
    token = service.db.session(42, 42, command_target("instagram alice"))
    service.db.bind(token, 9)
    service.db.db.execute("UPDATE follow_sessions SET features=? WHERE id=?",
                          (json.dumps(["posts", "reels"]), token))
    row = service.db.get_session(token, 42, 42, 9)
    fid = service.db.save(row)
    current = [("posts", "https://www.instagram.com/p/old/", "old")]
    async def items(_target, kind="posts"):
        return list(current) if kind == "posts" else []
    monkeypatch.setattr("mikachan.follow.public_items", items)
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await service.check(row)
    assert bot.messages == []
    assert {r[0] for r in service.db.db.execute(
        "SELECT category FROM follow_baselines WHERE follow_id=?", (fid,))} == {"posts", "reels"}
    current.insert(0, ("posts", "https://www.instagram.com/p/new/", "new"))
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await service.check(row)
    assert len(bot.messages) == 1
    assert "new" in bot.messages[0][1]
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await service.check(row)
    assert len(bot.messages) == 1


@pytest.mark.asyncio
async def test_media_mode_now_sends_two_button_notice(monkeypatch, tmp_path):
    store = Store(tmp_path / "db.sqlite")
    bot = type("Bot", (), {"send_message": AsyncMock(
        return_value=SimpleNamespace(message_id=7))})()
    service = FollowService(store, bot)
    token = service.db.session(42, 42, command_target("twitter alice"))
    service.db.bind(token, 9)
    service.db.db.execute("UPDATE follow_sessions SET features=?,mode=? WHERE id=?",
        (json.dumps(["tweets"]), "media", token))
    fid = service.db.save(service.db.get_session(token, 42, 42, 9))
    assert service.db.db.execute("SELECT mode FROM follows WHERE id=?", (fid,)).fetchone()[0] == "notice"
    current = [("tweets", "https://x.com/alice/status/1", "hello")]
    async def items(_target, kind="posts"):
        return list(current)
    monkeypatch.setattr("mikachan.follow.public_items", items)
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await service.check(row)
    current.insert(0, ("tweets", "https://x.com/alice/status/2", "next"))
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await service.check(row)
    assert store.jobs(42) == []
    assert bot.send_message.await_count == 1
    assert len(bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0]) == 2


@pytest.mark.asyncio
async def test_media_follow_outbox_retries_only_proven_failure(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    service = FollowService(store, SimpleNamespace())
    target = command_target("tiktok alice")
    token = service.db.session(42, 42, target)
    service.db.bind(token, 9)
    service.db.db.execute("UPDATE follow_sessions SET mode='media' WHERE id=?", (token,))
    fid = service.db.save(service.db.get_session(token, 42, 42, 9))
    url = "https://www.tiktok.com/@alice/video/1234567890123456789"
    key = f"posts:{url}"
    # Legacy queued jobs from an older release must still be retried safely.
    first = store.create_job(42, 42, url)
    service.db.db.execute("INSERT INTO follow_media_jobs VALUES (?,?,?,?,?,?)",
                          (fid, key, url, first, 1, 0))
    assert not store.db.in_transaction
    assert len(store.jobs(42)) == 1
    first = store.jobs(42)[0]["id"]
    assert store.transition(first, "QUEUED", "FAILED", error="network")
    service.db.db.execute("UPDATE follow_media_jobs SET next_retry=0 WHERE follow_id=?", (fid,))
    assert service.retry_failed_media()
    assert not store.db.in_transaction
    assert len(store.jobs(42)) == 2
    second = store.jobs(42)[0]["id"]
    assert second != first
    assert store.transition(second, "QUEUED", "DELIVERY_UNCERTAIN")
    service.db.db.execute("UPDATE follow_media_jobs SET next_retry=0 WHERE follow_id=?", (fid,))
    assert not service.retry_failed_media()
    assert len(store.jobs(42)) == 2


@pytest.mark.asyncio
async def test_profile_checks_survive_timeline_failure(monkeypatch, tmp_path):
    store = Store(tmp_path / "db.sqlite")
    class Bot:
        async def send_message(self, *_args, **_kwargs):
            raise AssertionError("the first check must not notify")
    service = FollowService(store, Bot())
    token = service.db.session(42, 42, command_target("twitter alice"))
    service.db.bind(token, 9)
    service.db.db.execute("UPDATE follow_sessions SET features=? WHERE id=?",
                          (json.dumps(["tweets", "bio"]), token))
    fid = service.db.save(service.db.get_session(token, 42, 42, 9))
    async def broken_timeline(_target, kind="posts"):
        raise ValueError("public timeline unavailable")
    async def profile(_target):
        return {"bio": "public bio"}
    monkeypatch.setattr("mikachan.follow.public_items", broken_timeline)
    monkeypatch.setattr("mikachan.follow.public_profile", profile)
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await service.check(row)
    assert {r[0] for r in service.db.db.execute(
        "SELECT category FROM follow_baselines WHERE follow_id=?", (fid,))} == {"bio"}


@pytest.mark.asyncio
async def test_posts_published_during_first_provider_failure_are_not_lost(monkeypatch, tmp_path):
    store = Store(tmp_path / "db.sqlite")
    bot = type("Bot", (), {"send_message": AsyncMock(
        return_value=SimpleNamespace(message_id=7))})()
    service = FollowService(store, bot)
    token = service.db.session(42, 42, command_target("twitter alice"))
    service.db.bind(token, 9)
    service.db.db.execute("UPDATE follow_sessions SET features=? WHERE id=?",
                          (json.dumps(["tweets"]), token))
    fid = service.db.save(service.db.get_session(token, 42, 42, 9))
    recent = ("tweets", "https://x.com/alice/status/2111998764345069830", "new")
    assert _post_timestamp(recent[1]) == 1792374719.116
    service.db.db.execute("UPDATE follow_start SET started=? WHERE follow_id=?",
                          (1792374000, fid))
    monkeypatch.setattr("mikachan.follow.public_items", AsyncMock(side_effect=ValueError("blocked")))
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    with pytest.raises(ValueError, match="no public source accessible"):
        await service.check(row)
    monkeypatch.setattr("mikachan.follow.public_items", AsyncMock(return_value=[recent]))
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await service.check(row)
    assert bot.send_message.await_count == 1
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await service.check(row)
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_failed_follow_alert_is_cooled_down(monkeypatch, tmp_path):
    store = Store(tmp_path / "db.sqlite")
    bot = type("Bot", (), {"send_message": AsyncMock()})()
    service = FollowService(store, bot)
    row = {"id": "follow", "chat_id": 42, "handle": "alice", "failures": 2}
    await service._failed(row)
    await service._failed(row)
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_structured_bio_and_stable_avatar(monkeypatch):
    monkeypatch.setattr("mikachan.follow.validate_dns", lambda _url: None)
    async def local_thread(fn, *args):
        return fn(*args)
    monkeypatch.setattr("mikachan.follow.asyncio.to_thread", local_thread)
    async def extract(_command, _timeout):
        return [[2, {"full_name": "Alice", "biography": "My actual bio",
                          "profile_pic_url": "https://cdn.example/a.jpg?expires=1"}]]
    monkeypatch.setattr("mikachan.follow._extract", extract)
    value = await public_profile(command_target("instagram alice"))
    assert value == {"name": "Alice", "bio": "My actual bio",
                     "avatar": "https://cdn.example/a.jpg"}
