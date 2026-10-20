"""Offline regression coverage for provider routing and user-requested alerts."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from mikachan.store import Store
from mikachan import follow as f


def saved(store, platform="instagram", features=None):
    service = f.FollowService(store, SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=77))))
    token = service.db.session(42, 42, f.command_target(f"{platform} alice"))
    service.db.bind(token, 7)
    if features is not None:
        service.db.db.execute("UPDATE follow_sessions SET features=? WHERE id=?",
                              (json.dumps(features), token))
    fid = service.db.save(service.db.get_session(token, 42, 42, 7))
    return service, fid


def test_new_follows_default_all_selected_and_notice(tmp_path):
    service, fid = saved(Store(tmp_path / "bot.db"), "instagram")
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    assert set(json.loads(row["features"])) == set(f.FEATURES["instagram"])
    assert row["mode"] == "notice"


def test_existing_db_upgrades_without_losing_follows(tmp_path):
    store = Store(tmp_path / "bot.db")
    service, fid = saved(store, "twitter", ["tweets"])
    store.db.execute("UPDATE follows SET mode='media' WHERE id=?", (fid,))
    f.FollowStore(store.db)
    assert store.db.execute("SELECT mode FROM follows WHERE id=?", (fid,)).fetchone()[0] == "notice"
    assert {x[1] for x in store.db.execute("PRAGMA table_info(follow_downloads)")} >= {"kind", "value"}


def test_follow_only_cookie_files_require_private_owner_permissions(monkeypatch, tmp_path):
    path = tmp_path / "x-cookies.txt"
    path.write_text("# Netscape HTTP Cookie File\n")
    path.chmod(0o600)
    monkeypatch.setenv("MIKACHAN_X_FOLLOW_COOKIES_FILE", str(path))
    assert f._follow_cookie_args("https://x.com/alice/tweets") == ["--cookies", str(path)]
    assert f._follow_cookie_args("https://www.youtube.com/@alice/videos") == []
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        f._follow_cookie_args("https://x.com/alice/tweets")


@pytest.mark.asyncio
async def test_instagram_independent_reels_and_exactly_two_buttons(monkeypatch, tmp_path):
    service, fid = saved(Store(tmp_path / "bot.db"), "instagram", ["posts", "reels"])
    lists = {
        "posts": [("posts", "https://www.instagram.com/p/old/", "old")],
        "reels": [("reels", "https://www.instagram.com/reel/old/", "old reel")],
    }
    called = []
    async def items(target, kind="posts"):
        called.append(kind)
        return lists[kind]
    monkeypatch.setattr(f, "public_items", items)
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await service.check(row)
    assert service.bot.send_message.await_count == 0
    assert called == ["posts", "reels"]
    lists["reels"].insert(0, ("reels", "https://www.instagram.com/reel/new/", "New reel"))
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await service.check(row)
    assert service.bot.send_message.await_count == 1
    message = service.bot.send_message.await_args
    buttons = message.kwargs["reply_markup"].inline_keyboard
    assert len(buttons) == 1 and len(buttons[0]) == 2
    assert buttons[0][0].callback_data.startswith("fd:")
    assert buttons[0][1].url == "https://www.instagram.com/reel/new/"
    stored = service.db.db.execute("SELECT kind,value FROM follow_downloads").fetchone()
    assert tuple(stored) == ("reels", "New reel")


@pytest.mark.asyncio
async def test_profile_change_sends_get_snapshot_and_link(monkeypatch, tmp_path):
    service, fid = saved(Store(tmp_path / "bot.db"), "twitter", ["bio"])
    profile = {"bio": "First"}
    async def profile_fn(_):
        return dict(profile)
    monkeypatch.setattr(f, "public_profile", profile_fn)
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await service.check(row)
    profile["bio"] = "Changed bio"
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await service.check(row)
    assert service.bot.send_message.await_count == 1
    buttons = service.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    assert len(buttons) == 1 and len(buttons[0]) == 2
    entry = service.db.db.execute("SELECT kind,value FROM follow_downloads").fetchone()
    assert tuple(entry) == ("bio", "Changed bio")


@pytest.mark.asyncio
async def test_unavailable_source_preserves_baseline_and_reason(monkeypatch, tmp_path):
    service, fid = saved(Store(tmp_path / "bot.db"), "instagram", ["posts", "reels"])
    async def items(_, kind="posts"):
        if kind == "posts":
            raise ValueError("source refused HTTP 403")
        return [("reels", "https://www.instagram.com/reel/valid/", "valid")]
    monkeypatch.setattr(f, "public_items", items)
    row = service.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    health = await service.check(row)
    assert health == {"posts":False,"reels":True}
    errors = service.db.db.execute("SELECT category,reason FROM follow_source_errors").fetchall()
    assert tuple(errors[0]) == ("posts", "source refused HTTP 403")
    baseline = {r[0] for r in service.db.db.execute("SELECT category FROM follow_baselines")}
    assert baseline == {"reels"}


@pytest.mark.asyncio
async def test_youtube_live_requires_confirmed_live_status(monkeypatch):
    monkeypatch.setattr(f, "validate_dns", lambda _: None)
    target = f.command_target("youtube alice")
    async def extracting(_cmd, _timeout):
        return {"entries": [
            {"id": "abcdefghijk", "title": "Live", "live_status": "is_live"},
            {"id": "12345678901", "title": "Old", "live_status": "was_live"},
        ]}
    monkeypatch.setattr(f, "_extract", extracting)
    assert await f.public_items(target, "live") == [
        ("live", "https://www.youtube.com/watch?v=abcdefghijk", "Live")]
    async def unknown(_cmd, _timeout):
        return {"entries": [{"id": "abcdefghijk"}]}
    monkeypatch.setattr(f, "_extract", unknown)
    with pytest.raises(ValueError, match="trustworthy"):
        await f.public_items(target, "live")


@pytest.mark.asyncio
async def test_instagram_reels_use_dedicated_extractor(monkeypatch):
    monkeypatch.setattr(f, "validate_dns", lambda _: None)
    urls = []
    async def extracting(cmd, _timeout):
        urls.append(cmd[-1])
        return [[2,{"post_shortcode":"abc", "type":"reel"}]]
    monkeypatch.setattr(f, "_extract", extracting)
    items = await f.public_items(f.command_target("instagram alice"), "reels")
    assert urls == ["https://www.instagram.com/alice/reels/"]
    assert items == [("reels", "https://www.instagram.com/reel/abc/", "")]


def test_legacy_download_token_table_receives_migration(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.db.execute("CREATE TABLE follow_downloads (token TEXT PRIMARY KEY,"
                     "follow_id TEXT NOT NULL,user_id INTEGER NOT NULL,"
                     "url TEXT NOT NULL,expires REAL NOT NULL)")
    f.FollowStore(store.db)
    columns = {r[1] for r in store.db.execute("PRAGMA table_info(follow_downloads)")}
    assert {"token", "kind", "value"} <= columns


@pytest.mark.asyncio
async def test_revoked_private_delivery_does_not_erase_follow(tmp_path):
    import asyncio
    store = Store(tmp_path / "bot.db")
    service, fid = saved(store, "twitter", ["tweets"])
    # Simulate temporary permission suspension at the time of a scheduled poll.
    store.access_registry = SimpleNamespace(
        module_enabled=lambda _name: True,
        can_access=lambda *_args: False,
    )
    task = asyncio.create_task(service.loop())
    try:
        for _ in range(50):
            await asyncio.sleep(.01)
            row = store.db.execute("SELECT next_check FROM follows WHERE id=?", (fid,)).fetchone()
            if row and row[0] > f.time.time() + 30:
                break
        assert store.db.execute("SELECT count(*) FROM follows WHERE id=?", (fid,)).fetchone()[0] == 1
        assert row and row[0] > f.time.time() + 30
    finally:
        service.running = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
