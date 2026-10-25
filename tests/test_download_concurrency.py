"""Collection cancellation, concurrent scheduling, and bounded video output."""

import asyncio
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendVideo

from mikachan.config import Settings
from mikachan.jobs import Worker
from mikachan.store import Store
from mikachan.media import verify


def _collection(store: Store, count: int = 87) -> str:
    cid = store.create_collection(42, 42, "Playlist", [
        {"title": f"Track {n}", "url": f"https://example.com/{n}"}
        for n in range(count)
    ])
    store.db.execute("UPDATE collections SET message_id=123 WHERE id=?", (cid,))
    for n in range(count):
        _jid, created = store.select_collection(cid, 42, 42, n, "audio")
        assert created
    return cid


def test_collection_stop_is_owned_and_keeps_current_item(tmp_path):
    store = Store(tmp_path / "bot.sqlite")
    cid = _collection(store)
    active = store.next_jobs()[0]
    assert store.transition(active["id"], "QUEUED", "DOWNLOADING")
    assert store.cancel_collection(cid, 43, 42, 123) is None
    assert store.cancel_collection(cid, 42, 42, 124) is None
    assert store.cancel_collection(cid, 42, 42, 123) == 86
    assert store.job(active["id"])["state"] == "DOWNLOADING"
    assert store.collection_batch(cid, 42, 42) == (87, 1, True)
    assert store.select_collection(cid, 42, 42, 0) == (None, False)
    assert store.next_jobs() == []


@pytest.mark.asyncio
async def test_playlist_uses_one_slot_while_two_new_requests_start(tmp_path):
    store = Store(tmp_path / "bot.sqlite")
    cid = _collection(store)
    launched = []
    waiting = asyncio.Event()
    worker = Worker(SimpleNamespace(max_active_downloads=3, max_active_per_user=3),
                    store, SimpleNamespace())

    async def hold(jid):
        launched.append(jid)
        await waiting.wait()

    worker._run = hold
    loop = asyncio.create_task(worker.loop())
    try:
        await asyncio.sleep(0.6)
        assert len(launched) == 1
        youtube = store.create_job(42, 42, "https://www.youtube.com/watch?v=abcdefghijk")
        third = store.create_job(42, 42, "https://example.com/other")
        await asyncio.sleep(0.6)
        assert set(launched) == {launched[0], youtube, third}
        assert store.db.execute(
            "SELECT count(*) FROM collection_selections s JOIN jobs j ON j.id=s.job_id "
            "WHERE s.collection_id=? AND j.state='RESOLVING'", (cid,)
        ).fetchone()[0] == 1
    finally:
        await worker.stop()
        await loop


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="ffmpeg tools unavailable")
async def test_oversize_video_is_fitted_to_real_mp4(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "testsrc2=size=640x360:rate=30", "-t", "5", "-c:v", "libx264",
        "-preset", "ultrafast", str(source),
    ], check=True)
    assert source.stat().st_size > 600_000
    worker = Worker(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    result = await worker._fit_video(source, 600_000, "480")
    assert result.stat().st_size <= 600_000
    assert (await verify(result, 600_000))["video"]
    assert not source.exists()


def test_three_slots_are_valid_configuration():
    settings = Settings(bot_token="123:abc", owner_telegram_id=42,
                        allowed_telegram_ids="42", max_active_downloads=3,
                        max_active_per_user=3)
    assert settings.max_active_downloads == settings.max_active_per_user == 3


@pytest.mark.asyncio
async def test_heavy_progress_uses_measured_bytes_and_only_exact_percent(tmp_path, monkeypatch):
    import mikachan.jobs as jobs

    worker = Worker(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    jid = "heavy-job"
    folder = tmp_path / jid
    worker._heavy_progress[jid] = (42, 0, 0.0, 0)
    emitted = []

    async def capture(_jid, _chat, value):
        emitted.append(value)

    monkeypatch.setattr(worker, "_progress", capture)
    monkeypatch.setattr(jobs.time, "monotonic", lambda: 10.0)
    await worker._bytes_progress(folder, 13_500_000, 168_750_000)
    assert emitted == ["⬇️ 13.5 MB · 8%"]
    await worker._bytes_progress(folder, 13_600_000, 168_750_000)
    assert len(emitted) == 1  # edits are bounded during fast downloads
    await worker._bytes_progress(folder, 18_000_000, None)
    assert emitted[-1] == "⬇️ 18.0 MB"


@pytest.mark.asyncio
async def test_ytdlp_progress_is_piped_and_updates_as_lines_arrive(tmp_path, monkeypatch):
    import mikachan.jobs as jobs

    monkeypatch.setattr(jobs, "validate_dns", lambda _url: None)
    command = []
    process = SimpleNamespace(
        stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader(), returncode=0,
        wait=AsyncMock(return_value=0),
    )
    process.stdout.feed_data(b"MIKACHAN_PROGRESS:13500000:168750000\n")
    process.stdout.feed_eof()
    process.stderr.feed_eof()

    async def launch(*args, **kwargs):
        command.extend(args)
        assert kwargs["stdout"] == asyncio.subprocess.PIPE
        (tmp_path / "01.mp4").write_bytes(b"fixture")
        return process

    monkeypatch.setattr(jobs.asyncio, "create_subprocess_exec", launch)
    worker = Worker(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    worker._heavy_progress[tmp_path.name] = (42, 0, 0.0, 0)
    worker._progress = AsyncMock()
    paths = await worker._ytdlp("https://www.youtube.com/watch?v=abcdefghijk",
                                tmp_path, 200_000_000, "720")
    assert paths == [tmp_path / "01.mp4"]
    assert "--progress-template" in command
    worker._progress.assert_awaited_once_with(tmp_path.name, 42, "⬇️ 13.5 MB · 8%")


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="ffmpeg tools unavailable")
@pytest.mark.parametrize("reject_video", [False, True])
async def test_media_is_deleted_and_second_delivery_uses_telegram_file_id(
        tmp_path, reject_video):
    fixture = tmp_path / "fixture.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "testsrc2=size=320x180:rate=24", "-t", "1", "-c:v", "libx264", str(fixture),
    ], check=True)
    settings = Settings(bot_token="123:abc", owner_telegram_id=42,
                        allowed_telegram_ids="42", data_dir=tmp_path / "data")
    store = Store(tmp_path / "data" / "bot.sqlite")
    sent = SimpleNamespace(message_id=300,
                           video=None if reject_video else SimpleNamespace(file_id="telegram-file-id"),
                           document=SimpleNamespace(file_id="telegram-file-id") if reject_video else None,
                           audio=None, photo=None)
    video_send = AsyncMock(
        side_effect=TelegramBadRequest(SendVideo(chat_id=42, video="file"), "video rejected")
        if reject_video else None, return_value=sent,
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=200)),
        edit_message_text=AsyncMock(), delete_message=AsyncMock(),
        send_video=video_send, send_document=AsyncMock(return_value=sent),
    )
    worker = Worker(settings, store, bot)

    async def local_download(_url, directory, _max_bytes, _mode):
        target = directory / "01.mp4"
        shutil.copyfile(fixture, target)
        return [target]

    worker._ytdlp = AsyncMock(side_effect=local_download)
    url = "https://www.tiktok.com/@alice/video/1234567890123456789"
    for _ in range(2):
        jid = store.create_job(42, 42, url)
        assert store.transition(jid, "QUEUED", "RESOLVING")
        await worker._run(jid)
        assert store.job(jid)["state"] == "COMPLETED"
        assert not (settings.data_dir / "jobs" / jid).exists()
    assert worker._ytdlp.await_count == 1
    if reject_video:
        assert bot.send_document.await_args_list[1].args[1] == "telegram-file-id"
    else:
        assert bot.send_video.await_args_list[1].args[1] == "telegram-file-id"


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="ffmpeg tools unavailable")
@pytest.mark.parametrize("url,mode", [
    ("https://www.youtube.com/watch?v=abcdefghijk", "1080"),
    ("https://www.pornhub.com/view_video.php?viewkey=6ac5dd5aca7cb", "720"),
])
async def test_selected_video_uses_userbot_even_below_public_upload_limit(tmp_path, url, mode):
    fixture = tmp_path / "small.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=24", "-t", "1", "-c:v", "libx264",
                    str(fixture)], check=True)
    settings = Settings(bot_token="123:abc", owner_telegram_id=42,
                        allowed_telegram_ids="42", data_dir=tmp_path / "data")
    store = Store(settings.data_dir / "bot.sqlite")
    sent = SimpleNamespace(message_id=300, video=SimpleNamespace(file_id="staged-file-id"),
                           document=None, audio=None, photo=None)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=200)),
                          edit_message_text=AsyncMock(), delete_message=AsyncMock(),
                          send_video=AsyncMock(), send_document=AsyncMock())
    worker = Worker(settings, store, bot)
    worker.userbot = SimpleNamespace(upload_and_copy=AsyncMock(return_value=sent))

    async def local_download(_url, directory, _max_bytes, _mode):
        target = directory / "01.mp4"
        shutil.copyfile(fixture, target)
        return [target]

    worker._ytdlp = AsyncMock(side_effect=local_download)
    jid = store.create_job(42, 42, url, mode=mode)
    assert store.transition(jid, "QUEUED", "RESOLVING")
    await worker._run(jid)
    assert store.job(jid)["state"] == "COMPLETED"
    worker.userbot.upload_and_copy.assert_awaited_once()
    bot.send_video.assert_not_awaited()
    assert not (settings.data_dir / "jobs" / jid).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("height", [480, 720, 1080])
async def test_pornhub_format_selection_caps_height_and_prefers_https(
        tmp_path, monkeypatch, height):
    import mikachan.jobs as jobs

    monkeypatch.setattr(jobs, "validate_dns", lambda _url: None)
    seen = []

    async def launch(*args, **_kwargs):
        seen.extend(args)
        (tmp_path / "01.mp4").write_bytes(b"fixture")
        return SimpleNamespace(communicate=AsyncMock(return_value=(b"", b"")), returncode=0)

    monkeypatch.setattr(jobs.asyncio, "create_subprocess_exec", launch)
    worker = Worker(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    paths = await worker._ytdlp(
        "https://www.pornhub.com/view_video.php?viewkey=6ac5dd5aca7cb",
        tmp_path, 10_000_000, str(height))
    assert paths == [tmp_path / "01.mp4"]
    fmt = seen[seen.index("-f") + 1]
    assert fmt.startswith(f"b[height<={height}][ext=mp4][protocol=https]")
    assert f"[height<={height}]" in fmt
    assert "--impersonate" not in seen  # The CDN rejected that fingerprint with HTTP 474.


@pytest.mark.asyncio
async def test_pornhub_410_refetches_signed_cdn_url_once(tmp_path, monkeypatch):
    import mikachan.jobs as jobs
    worker = Worker(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    stale = tmp_path / "stale.part"
    stale.write_bytes(b"partial")
    replacement = tmp_path / "fresh.mp4"
    async def download(*_args):
        if download.calls == 0:
            download.calls += 1
            raise ValueError("Pornhub media CDN returned HTTP 410 to this server; the URL was refreshed once")
        download.calls += 1
        replacement.write_bytes(b"video")
        return [replacement]
    download.calls = 0
    worker._ytdlp = download
    monkeypatch.setattr(jobs.asyncio, "sleep", AsyncMock())
    result = await worker._ytdlp_with_fresh_url(
        "https://www.pornhub.com/view_video.php?viewkey=6ac5dd5aca7cb",
        tmp_path, 10_000_000, "480")
    assert result == [replacement] and download.calls == 2
    assert not stale.exists()
