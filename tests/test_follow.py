import json

import pytest

from mikachan.follow import (FollowService, FollowStore, command_target,
                               creator_from_post, profile_target, public_profile,
                               _gallery_items)
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
    assert set(json.loads(row["features"])) == {"posts"}
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
    row = service.db.get_session(token, 42, 42, 9)
    fid = service.db.save(row)
    current = [("posts", "https://www.instagram.com/p/old/", "old")]
    async def items(_target, kind="posts"):
        return list(current)
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
async def test_media_mode_queues_public_post(monkeypatch, tmp_path):
    store = Store(tmp_path / "db.sqlite")
    class Bot:
        async def send_message(self, *_args, **_kwargs):
            raise AssertionError("media mode should queue a job")
    service = FollowService(store, Bot())
    token = service.db.session(42, 42, command_target("twitter alice"))
    service.db.bind(token, 9)
    service.db.db.execute("UPDATE follow_sessions SET mode='media' WHERE id=?", (token,))
    fid = service.db.save(service.db.get_session(token, 42, 42, 9))
    current = [("tweets", "https://x.com/alice/status/1", "hello")]
    async def items(_target, kind="posts"):
        return list(current)
    monkeypatch.setattr("mikachan.follow.public_items", items)
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await service.check(row)
    current.insert(0, ("tweets", "https://x.com/alice/status/2", "next"))
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await service.check(row)
    jobs = store.jobs(42)
    assert len(jobs) == 1
    assert jobs[0]["url"] == "https://x.com/alice/status/2"


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
