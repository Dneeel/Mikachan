"""Owner source controls and private local-book result boundaries."""

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mikachan.local_sources import LocalSources
from mikachan.now_service import NowService
from mikachan.settings_ui import SettingsUI
from mikachan.store import Store


def bridge():
    client = SimpleNamespace(get_dialogs=AsyncMock(return_value=[
        SimpleNamespace(id=-10055, entity=SimpleNamespace(id=55))]))
    return SimpleNamespace(lock=asyncio.Lock(), _ready=AsyncMock(), client=client)


@pytest.mark.asyncio
async def test_source_membership_categories_suspend_and_remove(tmp_path):
    db = Store(tmp_path / "state.sqlite").db
    provider = bridge()
    sources = LocalSources(db, provider, None, tmp_path)
    await sources.upsert(-10055, "Books", "book,music")
    row = db.execute("SELECT * FROM local_sources").fetchone()
    assert row["categories"] == "book,music" and row["status"] == "ACTIVE"
    with pytest.raises(ValueError, match="join"):
        await sources.upsert(-10077, "Unknown", "book")
    with pytest.raises(ValueError, match="Categories"):
        await sources.upsert(-10055, "Bad", "unknown")
    sources.set_status(-10055, "SUSPENDED")
    assert db.execute("SELECT status FROM local_sources").fetchone()[0] == "SUSPENDED"
    sources.remove(-10055)
    assert db.execute("SELECT COUNT(*) FROM local_sources").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_book_search_bound_callback_and_temp_cleanup(tmp_path):
    db = Store(tmp_path / "state.sqlite").db
    provider = bridge()
    sources = LocalSources(db, provider, None, tmp_path)
    await sources.upsert(-10055, "Library", "book")
    file_ref = SimpleNamespace(name="Novel.pdf")
    hit = SimpleNamespace(id=19, document=SimpleNamespace(size=50), file=file_ref)
    async def hits(*_args, **_kwargs):
        yield hit
    provider.client.iter_messages = hits
    provider.client.get_messages = AsyncMock(return_value=hit)
    async def download(_msg, *, file, progress_callback):
        progress_callback(15, 15)
        target = Path(file)
        target.write_bytes(b"%PDF-real fixture")
        return str(target)
    provider.client.download_media = download
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
                          send_document=AsyncMock(), edit_message_text=AsyncMock())
    sources.bot = bot
    await sources.show_books(7, 8, "Novel")
    button = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    query = SimpleNamespace(data=button.callback_data, from_user=SimpleNamespace(id=9),
                            message=SimpleNamespace(chat=SimpleNamespace(id=7), message_id=42),
                            answer=AsyncMock())
    assert await sources.callback(query)
    bot.send_document.assert_not_awaited()
    query.from_user.id = 8
    assert await sources.callback(query)
    bot.send_document.assert_awaited_once()
    assert "✅ Sent" in bot.edit_message_text.await_args.args[0]
    assert list((tmp_path / "jobs").iterdir()) == []


@pytest.mark.asyncio
async def test_owner_settings_local_source_capture_and_suspension(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    store.seed_access({1})
    store.bootstrap_access(1)
    provider = bridge()
    sources = LocalSources(store.db, provider, None, tmp_path)
    ui = SettingsUI(store.db, store.access_registry, NowService(store.db),
                    local_sources=sources)
    sent = SimpleNamespace(message_id=50)
    owner_chat = SimpleNamespace(id=1, type="private")
    opener = SimpleNamespace(from_user=SimpleNamespace(id=1), chat=owner_chat,
                             reply=AsyncMock(return_value=sent))
    await ui.show(opener)
    buttons = opener.reply.await_args.kwargs["reply_markup"]
    assert any(button.text == "📂 Local Sources" for row in buttons.inline_keyboard
               for button in row)
    token = buttons.inline_keyboard[0][0].callback_data.split(":")[1]
    store.db.execute("UPDATE settings_sessions SET page='locals' WHERE token=?", (token,))
    store.db.execute("INSERT INTO settings_captures VALUES (?,?,?,?,?,?)",
                     (1, 1, 51, token, "local_add", __import__("time").time() + 300))
    reply = SimpleNamespace(from_user=SimpleNamespace(id=1), chat=owner_chat,
        text="-10055 | Library | book,podcast",
        reply_to_message=SimpleNamespace(message_id=51),
        reply=AsyncMock(return_value=SimpleNamespace(message_id=52)))
    assert await ui.reply(reply)
    assert store.db.execute("SELECT categories FROM local_sources").fetchone()[0] == \
           "book,podcast"
    sources.set_status(-10055, "SUSPENDED")
    assert store.db.execute("SELECT status FROM local_sources").fetchone()[0] == "SUSPENDED"


@pytest.mark.asyncio
async def test_local_music_uses_validated_audio_and_cleans_files(tmp_path):
    sample = tmp_path / "tone.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=16000", "-t", "0.5", str(sample)],
                   check=True)
    db = Store(tmp_path / "state.sqlite").db
    provider = bridge()
    sources = LocalSources(db, provider, None, tmp_path)
    await sources.upsert(-10055, "Music", "music")
    hit = SimpleNamespace(id=19, document=SimpleNamespace(size=sample.stat().st_size),
                          file=SimpleNamespace(name="tone.mp3"))
    async def hits(*_args, **_kwargs):
        yield hit
    provider.client.iter_messages = hits
    provider.client.get_messages = AsyncMock(return_value=hit)
    async def download(_msg, *, file, progress_callback):
        data = sample.read_bytes()
        progress_callback(len(data), len(data))
        Path(file).write_bytes(data)
        return file
    provider.client.download_media = download
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
                          send_audio=AsyncMock(), edit_message_text=AsyncMock())
    sources.bot = bot
    await sources.show_files(7, 8, "tone", "music")
    button = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    query = SimpleNamespace(data=button.callback_data, from_user=SimpleNamespace(id=8),
                            message=SimpleNamespace(chat=SimpleNamespace(id=7), message_id=42),
                            answer=AsyncMock())
    assert await sources.callback(query)
    bot.send_audio.assert_awaited_once()
    assert list((tmp_path / "jobs").iterdir()) == []
