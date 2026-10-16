"""Bounded on-demand downloader and Telegram delivery."""

import asyncio
import hashlib
import html
import json
import os
import shutil
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from aiogram import Bot
from aiogram.types import (
    FSInputFile,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)

from .config import Settings
from .security import source, validate_dns, collection, clean_url
from .store import Store
from .media import groups, verify

MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
}


class Worker:
    def __init__(self, settings: Settings, store: Store, bot: Bot):
        self.settings, self.store, self.bot = settings, store, bot
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.running_users: set[int] = set()
        self.running = False

    async def loop(self) -> None:
        self.running = True
        while self.running:
            if len(self.tasks) < self.settings.max_active_downloads:
                rows = self.store.next_jobs()
                for row in rows:
                    if len(self.tasks) >= self.settings.max_active_downloads:
                        break
                    if row["user_id"] not in self.running_users:
                        old = row["state"]
                        if self.store.transition(row["id"], old, "RESOLVING"):
                            self.running_users.add(row["user_id"])
                            task = asyncio.create_task(self._run(row["id"]))
                            self.tasks[row["id"]] = task
                            task.add_done_callback(
                                partial(self._task_done, str(row["id"]), int(row["user_id"]))
                            )
            await asyncio.sleep(1)

    def _done(self, jid: str, user: int) -> None:
        self.tasks.pop(jid, None)
        self.running_users.discard(user)

    def _task_done(self, jid: str, user: int, task: asyncio.Task[None]) -> None:
        self._done(jid, user)
        if not task.cancelled():
            task.exception()

    async def stop(self) -> None:
        self.running = False
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def _run(self, jid: str) -> None:
        row = self.store.job(jid)
        if row is None:
            return
        url, chat = row["url"], row["chat_id"]
        job_dir = self.settings.data_dir / "jobs" / jid
        shutil.rmtree(job_dir, ignore_errors=True)
        job_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        max_bytes = (48 if self.settings.bot_api_mode == "public" else 1900) * 1024 * 1024
        try:
            key = hashlib.sha256((url + "|document|public").encode()).hexdigest()
            cached = self.store.db.execute(
                "SELECT file_id FROM cache WHERE key=? AND owner_id=?", (key, row["user_id"])
            ).fetchone()
            if cached:
                self.store.transition(jid, "RESOLVING", "DELIVERING")
                try:
                    sent = await self.bot.send_document(
                        chat, cached["file_id"], caption="MIKA NEXUS · " + html.escape(url[:900])
                    )
                except Exception as exc:
                    self.store.transition(
                        jid, "DELIVERING", "DELIVERY_UNCERTAIN", error=type(exc).__name__
                    )
                    return
                self.store.receipt(
                    jid, sent.message_id, sent.document.file_id if sent.document else None
                )
                self.store.transition(jid, "DELIVERING", "COMPLETED", receipt_id=sent.message_id)
                return
            if collection(url):
                items, title = await self._browse(url)
                if not items:
                    raise ValueError("No public collection items found")
                cid = self.store.create_collection(row["user_id"], chat, title, items)
                from .bot import kb

                caption = f"MIKA NEXUS · {html.escape(title[:120])}\nSelect one item:"
                buttons = [
                    [(f"{i + 1}. {item['title'][:45]}", f"v:{cid}:d:{i}")]
                    for i, item in enumerate(items[:10])
                ]
                if len(items) > 10:
                    buttons.append([("Next ▶", f"v:{cid}:p:1")])
                sent = await self.bot.send_message(chat, caption, reply_markup=kb(buttons))
                self.store.db.execute(
                    "UPDATE collections SET message_id=? WHERE id=?", (sent.message_id, cid)
                )
                self.store.transition(jid, "RESOLVING", "COMPLETED", receipt_id=sent.message_id)
                return
            self.store.transition(jid, "RESOLVING", "DOWNLOADING")
            if source(url) == "direct":
                paths = [await self._direct(url, job_dir, max_bytes)]
            else:
                paths = await self._ytdlp(url, job_dir, max_bytes)
            current_job = self.store.job(jid)
            if current_job and current_job["state"] == "CANCELLED":
                return
            self.store.transition(jid, "DOWNLOADING", "VERIFYING", artifact=str(job_dir))
            if sum(p.stat().st_size for p in paths) > max_bytes:
                raise ValueError("gallery exceeds Bot API mode limit")
            for path in paths:
                await verify(path, max_bytes)
            if not self.store.transition(jid, "VERIFYING", "DELIVERING"):
                return
            try:
                sent_messages = []
                for chunk in groups(paths):
                    if len(chunk) == 1:
                        sent_messages.append(
                            await self.bot.send_document(
                                chat,
                                FSInputFile(chunk[0]),
                                caption="MIKA NEXUS · " + html.escape(url[:900]),
                            )
                        )
                    else:
                        media: list[
                            InputMediaAudio | InputMediaDocument | InputMediaPhoto | InputMediaVideo
                        ] = [
                            InputMediaDocument(
                                media=FSInputFile(path),
                                caption=("MIKA NEXUS · " + html.escape(url[:900]))
                                if i == 0 and not sent_messages
                                else None,
                            )
                            for i, path in enumerate(chunk)
                        ]
                        sent_messages.extend(await self.bot.send_media_group(chat, media))
                    for sent in sent_messages[-len(chunk) :]:
                        self.store.receipt(
                            jid, sent.message_id, sent.document.file_id if sent.document else None
                        )
            except Exception as exc:
                self.store.transition(
                    jid, "DELIVERING", "DELIVERY_UNCERTAIN", error=type(exc).__name__
                )
                return
            self.store.transition(
                jid, "DELIVERING", "COMPLETED", receipt_id=sent_messages[0].message_id
            )
            if len(paths) == 1 and sent_messages[0].document:
                self.store.db.execute(
                    "INSERT OR REPLACE INTO cache VALUES (?,?,?,?,strftime('%s','now'))",
                    (key, sent_messages[0].document.file_id, "document", row["user_id"]),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            current = self.store.job(jid)
            if current and current["state"] not in {"CANCELLED", "DELIVERY_UNCERTAIN"}:
                safe = str(exc)[:180] if isinstance(exc, ValueError) else type(exc).__name__
                self.store.transition(jid, current["state"], "FAILED", error=safe)
                await self.bot.send_message(chat, f"MIKA NEXUS · Could not process link: {safe}")
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    async def _browse(self, url: str) -> tuple[list[dict[str, str]], str]:
        validate_dns(url)
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "--flat-playlist",
            "--playlist-end",
            "30",
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env={"PATH": os.environ.get("PATH", "")},
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 30)
        except (TimeoutError, asyncio.CancelledError):
            import signal

            os.killpg(proc.pid, signal.SIGTERM)
            await proc.wait()
            raise
        if proc.returncode:
            raise ValueError("public collection inspection unavailable")
        data = json.loads(out)
        items = []
        for entry in data.get("entries", []):
            if not entry:
                continue
            target = entry.get("webpage_url") or entry.get("url") or ""
            if source(url) == "youtube" and not str(target).startswith("http") and entry.get("id"):
                target = "https://www.youtube.com/watch?v=" + str(entry["id"])
            try:
                target = clean_url(str(target))
            except ValueError:
                continue
            items.append({"title": str(entry.get("title") or "Untitled")[:100], "url": target})
        return items[:30], str(data.get("title") or "Collection")

    async def _direct(self, url: str, directory: Path, max_bytes: int) -> Path:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for _ in range(5):
                validate_dns(url)
                async with client.stream("GET", url) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        target = resp.headers.get("location")
                        if not target:
                            raise ValueError("redirect without destination")
                        from urllib.parse import urljoin

                        url = urljoin(url, target)
                        continue
                    resp.raise_for_status()
                    mime = resp.headers.get("content-type", "").split(";")[0].lower()
                    if mime in {"text/html", "application/xhtml+xml"}:
                        raise ValueError("URL is a web page, not a direct file")
                    suffix = MIME_EXT.get(mime) or Path(urlsplit(url).path).suffix[:10] or ".bin"
                    if not suffix.startswith(".") or not suffix[1:].isalnum():
                        suffix = ".bin"
                    path = directory / ("artifact" + suffix)
                    total = 0
                    with path.open("wb") as f:
                        async for chunk in resp.aiter_bytes(65536):
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError("file exceeds Bot API mode limit")
                            f.write(chunk)
                    return path
        raise ValueError("too many redirects")

    async def _ytdlp(self, url: str, directory: Path, max_bytes: int) -> list[Path]:
        validate_dns(url)
        if not shutil.which("yt-dlp"):
            raise ValueError("yt-dlp is not installed")
        template = str(directory / "%(autonumber)02d.%(ext)s")
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "--playlist-end",
            "10",
            "--no-warnings",
            "--restrict-filenames",
            "--max-filesize",
            str(max_bytes),
            "-o",
            template,
            url,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env={"PATH": os.environ.get("PATH", "")},
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), 600)
        except (TimeoutError, asyncio.CancelledError):
            import signal

            os.killpg(proc.pid, signal.SIGTERM)
            await proc.wait()
            raise
        if proc.returncode:
            raise ValueError("source extractor could not access this public item")
        files = [p for p in directory.iterdir() if p.is_file() and not p.name.endswith(".part")]
        if not 1 <= len(files) <= 10:
            raise ValueError("source returned no bounded deliverable media")
        return sorted(files)
