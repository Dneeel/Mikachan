"""Owner-curated Telegram sources and bounded, user-bound local book retrieval."""

import asyncio
import html
import secrets
import sqlite3
import tempfile
import time
from pathlib import Path

from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from .media import verify

FORMATS = {"book": (".pdf", ".epub"),
           "music": (".mp3", ".m4a", ".ogg", ".flac"),
           "podcast": (".mp3", ".m4a", ".ogg", ".flac")}


class LocalSources:
    def __init__(self, db: sqlite3.Connection, bridge, bot, data_dir: Path):
        self.db, self.bridge, self.bot, self.data_dir = db, bridge, bot, data_dir
        db.executescript("""
        CREATE TABLE IF NOT EXISTS local_sources (
          chat_id INTEGER PRIMARY KEY, name TEXT NOT NULL, categories TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('ACTIVE','SUSPENDED')),
          updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS local_book_results (
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
          menu_id INTEGER, source_id INTEGER NOT NULL, source_message INTEGER NOT NULL,
          title TEXT NOT NULL, kind TEXT NOT NULL, expires REAL NOT NULL);
        """)

    async def validate_membership(self, chat_id: int) -> None:
        if self.bridge is None:
            raise ValueError("Connect the Telegram user account before adding local sources")
        if not -2**63 < chat_id < 0:
            raise ValueError("Use a negative Telegram group or channel ID")
        async with self.bridge.lock:
            await self.bridge._ready()
            dialogs = await self.bridge.client.get_dialogs(limit=500)
            if not any(getattr(dialog.entity, "id", None) == abs(chat_id) or
                       getattr(dialog, "id", None) == chat_id for dialog in dialogs):
                raise ValueError("The user account must join this chat or channel first "
                                 "(among its 500 most recent dialogs)")

    async def upsert(self, chat_id: int, name: str, categories: str) -> None:
        if not 1 <= len(name.strip()) <= 80:
            raise ValueError("Source name must contain 1–80 characters")
        kinds = {part.strip().lower() for part in categories.split(",")}
        if not kinds or not kinds <= {"book", "music", "podcast"}:
            raise ValueError("Categories must be book, music, podcast, or a comma-separated mix")
        await self.validate_membership(chat_id)
        self.db.execute("INSERT INTO local_sources VALUES (?,?,?,'ACTIVE',?) "
                        "ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name,"
                        "categories=excluded.categories,updated=excluded.updated",
                        (chat_id, name.strip(), ",".join(sorted(kinds)), time.time()))

    def set_status(self, chat_id: int, status: str) -> None:
        if status not in {"ACTIVE", "SUSPENDED"}:
            raise ValueError("Invalid source status")
        self.db.execute("UPDATE local_sources SET status=?,updated=? WHERE chat_id=?",
                        (status, time.time(), chat_id))

    def remove(self, chat_id: int) -> None:
        self.db.execute("DELETE FROM local_sources WHERE chat_id=?", (chat_id,))
        self.db.execute("DELETE FROM local_book_results WHERE source_id=?", (chat_id,))

    def has_active(self, kind: str) -> bool:
        return bool(self.db.execute("SELECT 1 FROM local_sources WHERE status='ACTIVE' "
            "AND (','||categories||',') LIKE ? LIMIT 1", (f"%,{kind},%",)).fetchone())

    async def search_files(self, query: str, kind: str) -> tuple[list[tuple[int, int, str]], int]:
        if kind not in FORMATS:
            raise ValueError("Unsupported local search type")
        if self.bridge is None:
            raise ValueError("Local search needs the Telegram user account login")
        sources = self.db.execute("SELECT chat_id FROM local_sources WHERE status='ACTIVE' "
                                  "AND (','||categories||',') LIKE ? "
                                  "ORDER BY chat_id LIMIT 20", (f"%,{kind},%",)).fetchall()
        if not sources:
            return [], 0
        hits: list[tuple[int, int, str]] = []
        failed = 0
        async with self.bridge.lock:
            await self.bridge._ready()
            for source in sources:
                source_id = int(source[0])
                try:
                    async with asyncio.timeout(20):
                        async for msg in self.bridge.client.iter_messages(
                                source_id, search=query, limit=15):
                            document = getattr(msg, "document", None)
                            filename = str(getattr(getattr(msg, "file", None), "name", "") or "")
                            size = int(getattr(document, "size", 0) or 0)
                            if document and filename.lower().endswith(FORMATS[kind]) and \
                                    0 < size <= 48 * 1024 * 1024:
                                hits.append((source_id, msg.id, filename[:120]))
                                if len(hits) >= 20:
                                    return hits, failed
                except Exception:
                    failed += 1
        return hits, failed

    async def search_books(self, query: str) -> tuple[list[tuple[int, int, str]], int]:
        return await self.search_files(query, "book")

    async def show_books(self, chat: int, user: int, query: str) -> None:
        await self.show_files(chat, user, query, "book")

    async def show_files(self, chat: int, user: int, query: str, kind: str) -> None:
        if not 1 <= len(query.strip()) <= 120:
            raise ValueError("Enter a title or author (up to 120 characters)")
        hits, failed = await self.search_files(query.strip(), kind)
        if not hits:
            if kind == "book":
                await self.bot.send_message(chat, "📚 No PDF or EPUB found in active local sources." +
                    (f" {failed} source(s) could not be searched." if failed else ""))
            return
        rows = []
        pending = []
        for source_id, message_id, title in hits:
            token = secrets.token_urlsafe(8)
            pending.append((token, user, chat, source_id, message_id, title, kind,
                            time.time() + 900))
            rows.append([InlineKeyboardButton(text=("📖 " if kind == "book" else "🎵 ") + title[:55],
                                              callback_data=f"lb:{token}")])
        sent = await self.bot.send_message(chat, "📂 <b>Local " + kind.title() + "</b> · " +
            html.escape(query[:100]) + (f"\n⚠️ {failed} source(s) unavailable." if failed else ""),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        self.db.executemany("INSERT INTO local_book_results VALUES (?,?,?,?,?,?,?,?,?)",
                            [(token, owner, target, sent.message_id, source, mid, name, file_kind, expiry)
                             for token, owner, target, source, mid, name, file_kind, expiry in pending])

    async def callback(self, query) -> bool:
        data = str(query.data or "")
        if not data.startswith("lb:"):
            return False
        row = self.db.execute("SELECT r.* FROM local_book_results r JOIN local_sources s "
            "ON s.chat_id=r.source_id WHERE r.token=? AND r.user_id=? AND r.chat_id=? "
            "AND r.menu_id=? AND r.expires>? AND s.status='ACTIVE'",
            (data[3:], query.from_user.id, query.message.chat.id,
             query.message.message_id, time.time())).fetchone()
        if not row:
            await query.answer("This book result expired or is unavailable")
            return True
        self.db.execute("DELETE FROM local_book_results WHERE token=?", (data[3:],))
        await query.answer("Sending file…")
        if self.bridge is None:
            return True
        title = html.escape(str(row["title"])[:100])
        progress = await self.bot.send_message(row["chat_id"], "⏳ Fetching · " + title)
        async def finish(text: str) -> None:
            try:
                await self.bot.edit_message_text(text, chat_id=row["chat_id"],
                                                 message_id=progress.message_id)
            except Exception:
                pass
        root = self.data_dir / "jobs"
        root.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="local-file-", dir=root) as temp:
                async with asyncio.timeout(180):
                    async with self.bridge.lock:
                        await self.bridge._ready()
                        msg = await self.bridge.client.get_messages(row["source_id"],
                                                                    ids=row["source_message"])
                        doc = getattr(msg, "document", None)
                        name = str(getattr(getattr(msg, "file", None), "name", "") or "")
                        kind = str(row["kind"])
                        if (not doc or kind not in FORMATS or
                                not name.lower().endswith(FORMATS[kind]) or
                                int(getattr(doc, "size", 0) or 0) > 48 * 1024 * 1024):
                            raise ValueError("Source file is no longer available")
                        def bounded(received: int, total: int) -> None:
                            if received > 48 * 1024 * 1024 or total > 48 * 1024 * 1024:
                                raise ValueError("Source file exceeds Telegram's hosted upload limit")
                        path = await self.bridge.client.download_media(
                            msg, file=str(Path(temp) / ("file" + Path(name).suffix.lower())),
                            progress_callback=bounded)
                if not path:
                    raise ValueError("Source file download failed")
                if kind == "book":
                    with Path(path).open("rb") as book_file:
                        header = book_file.read(5)
                    valid = (header.startswith(b"%PDF-") if name.lower().endswith(".pdf") else
                             header.startswith(b"PK\x03\x04"))
                    if not valid or Path(path).stat().st_size > 48 * 1024 * 1024:
                        raise ValueError("Book file format or size is invalid")
                    await finish("📤 Sending · " + title)
                    await self.bot.send_document(row["chat_id"], FSInputFile(path),
                                                 caption="📚 " + html.escape(name[:120]))
                else:
                    streams = await verify(Path(path), 48 * 1024 * 1024)
                    if not streams["audio"] or streams["video"]:
                        raise ValueError("Source file is not audio")
                    await finish("📤 Sending · " + title)
                    await self.bot.send_audio(row["chat_id"], FSInputFile(path),
                                              caption="🎵 " + html.escape(name[:120]))
            await finish("✅ Sent · " + title)
        except (ValueError, OSError) as exc:
            await finish("⚠️ " + html.escape(str(exc)[:180]))
        except Exception:
            await finish("⚠️ Local source is unavailable.")
        return True
