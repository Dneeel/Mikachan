"""Regressions for text posts, public post URLs, and bounded follow fallbacks."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from mikachan.follow import profile_target, public_items
from mikachan.jobs import Worker
from mikachan.public_social import public_x_post
from mikachan.security import instagram_cookie_args, links, source
from mikachan.store import Store


@pytest.mark.asyncio
async def test_text_only_x_status_is_delivered_as_text(monkeypatch, tmp_path):
    url = "https://x.com/alice/status/2103632534596632587"
    monkeypatch.setattr("mikachan.public_social._get", AsyncMock(return_value={
        "code": 200, "status": {"id": "2103632534596632587",
            "author": {"screen_name": "alice"}, "text": "A public text update", "media": {"all": []}},
    }))
    assert await public_x_post(url) == ("A public text update", [])
    store = Store(tmp_path / "db.sqlite")
    jid = store.create_job(42, 42, url)
    store.transition(jid, "QUEUED", "RESOLVING")

    class Bot:
        def __init__(self):
            self.messages = []

        async def send_message(self, _chat, body, **kwargs):
            self.messages.append((body, kwargs))
            return SimpleNamespace(message_id=len(self.messages))

        async def delete_message(self, *_args):
            return None

    bot = Bot()
    worker = Worker(SimpleNamespace(data_dir=tmp_path, bot_api_mode="public"), store, bot)
    worker._progress = AsyncMock()
    monkeypatch.setattr("mikachan.jobs.resolve_episode_link", AsyncMock(return_value=None))
    monkeypatch.setattr("mikachan.jobs.public_x_post", AsyncMock(
        return_value=("A public text update", [])))
    await worker._run(jid)
    assert store.job(jid)["state"] == "COMPLETED"
    assert "A public text update" in bot.messages[-1][0]
    labels = [button.text for row in bot.messages[-1][1]["reply_markup"].inline_keyboard
              for button in row]
    assert "🔗 Link" in labels and "👤 Profile" in labels


def test_spaced_danbooru_post_url_is_recovered():
    text = "https://danbooru.donmai.us/posts /6335171?q=parent%3A6335171"
    assert links(text, 5)[0] == [
        "https://danbooru.donmai.us/posts/6335171?q=parent%3A6335171"]
    assert source(links(text, 5)[0][0]) == "danbooru"


@pytest.mark.asyncio
async def test_danbooru_post_uses_matching_public_id(monkeypatch, tmp_path):
    post_url = "https://danbooru.donmai.us/posts/6335171?q=parent%3A6335171"
    monkeypatch.setattr("mikachan.jobs.validate_dns", lambda _url: None)
    original = httpx.AsyncClient

    def client(*args, **kwargs):
        return original(*args, trust_env=False, transport=httpx.MockTransport(lambda _request:
            httpx.Response(200, json={"id": 6335171, "is_deleted": False,
                "file_url": "https://cdn.donmai.us/original/example.jpg"})), **kwargs)

    monkeypatch.setattr("mikachan.jobs.httpx.AsyncClient", client)
    worker = Worker(SimpleNamespace(), Store(tmp_path / "db.sqlite"), SimpleNamespace())
    artifact = tmp_path / "image.jpg"
    worker._direct = AsyncMock(return_value=artifact)
    assert await worker._danbooru_post(post_url, tmp_path, 48_000_000) == artifact
    worker._direct.assert_awaited_once_with(
        "https://cdn.donmai.us/original/example.jpg", tmp_path, 48_000_000)
    assert json.loads((tmp_path / ".mikachan-meta.json").read_text())["title"] == (
        "Danbooru post 6335171")


@pytest.mark.asyncio
async def test_youtube_follow_uses_public_feed_without_channel_extractor(monkeypatch):
    channel = "UC" + "A" * 22
    video = "b" * 11
    target = profile_target(f"https://www.youtube.com/channel/{channel}")
    feed = (f'<feed xmlns="http://www.w3.org/2005/Atom" '
            f'xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
            f'<yt:channelId>{channel}</yt:channelId><entry><yt:videoId>{video}</yt:videoId>'
            '<title>New upload</title></entry></feed>')
    monkeypatch.setattr("mikachan.follow.validate_dns", lambda _url: None)
    async def local_thread(fn, *args):
        return fn(*args)
    monkeypatch.setattr("mikachan.follow.asyncio.to_thread", local_thread)
    original = httpx.AsyncClient

    def client(*args, **kwargs):
        return original(*args, trust_env=False, transport=httpx.MockTransport(lambda _request:
            httpx.Response(200, text=feed)), **kwargs)

    monkeypatch.setattr("mikachan.follow.httpx.AsyncClient", client)
    monkeypatch.setattr("mikachan.follow._extract", AsyncMock(
        side_effect=AssertionError("The channel extractor should not run")))
    assert await asyncio.wait_for(public_items(target), 5) == [
        ("videos", f"https://www.youtube.com/watch?v={video}", "New upload")]


def test_instagram_cookie_file_is_private_and_scoped(monkeypatch, tmp_path):
    path = tmp_path / "instagram.txt"
    path.write_text("# Netscape HTTP Cookie File\n")
    path.chmod(0o600)
    monkeypatch.setenv("MIKACHAN_INSTAGRAM_COOKIES_FILE", str(path))
    assert instagram_cookie_args("https://www.instagram.com/p/abc/") == [
        "--cookies", str(path)]
    assert instagram_cookie_args("https://x.com/alice/status/123") == []
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private regular file"):
        instagram_cookie_args("https://www.instagram.com/p/abc/")


@pytest.mark.asyncio
async def test_instagram_post_audio_selects_audio_asset_only(monkeypatch, tmp_path):
    from mikachan import jobs

    url = "https://www.instagram.com/reel/ABC123/"
    messages = [[3, "https://cdn.example/video.mp4", {"video_url": "https://cdn.example/video.mp4"}],
                [3, "https://cdn.example/sound.m4a", {
                    "audio_url": "https://cdn.example/sound.m4a", "audio_title": "Real sound"}]]

    class Process:
        returncode = 0

        async def communicate(self):
            return json.dumps(messages).encode(), b""

    monkeypatch.setattr(jobs, "validate_dns", lambda _url: None)
    monkeypatch.setattr(jobs.shutil, "which", lambda _name: "/bin/gallery-dl")
    spawn = AsyncMock(return_value=Process())
    monkeypatch.setattr(jobs.asyncio, "create_subprocess_exec", spawn)
    worker = Worker(SimpleNamespace(), Store(tmp_path / "db.sqlite"), SimpleNamespace())
    artifact = tmp_path / "sound.m4a"
    artifact.write_bytes(b"audio")
    worker._direct = AsyncMock(return_value=artifact)
    assert await worker._gallery_public(url, tmp_path, 1000, audio_only=True) == [artifact]
    assert worker._direct.await_args.args[0] == "https://cdn.example/sound.m4a"
    assert "extractor.instagram.audio=merged" in spawn.await_args.args
    assert json.loads((tmp_path / ".mikachan-meta.json").read_text())["audio_title"] == (
        "Real sound")
