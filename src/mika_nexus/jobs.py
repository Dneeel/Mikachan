"""Bounded on-demand downloader and Telegram delivery."""

import asyncio
import hashlib
import html
import json
import os
import re
import shutil
from functools import partial
from pathlib import Path
from urllib.parse import urljoin, urlsplit

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
from .catalog import resolve_collection, resolve_track
from .matching import match_score
from .search import Searcher
from .podcasts import apple_podcast_feed, episodes, is_feed_url

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


def extractor_error(url: str, stderr: bytes) -> str:
    """Summarize extractor failures without forwarding untrusted output or URLs."""
    detail = stderr.decode(errors="replace").lower()
    provider = source(url)
    if any(term in detail for term in ("cookies", "login", "authentication required")):
        return "This source requires login; anonymous download is unavailable"
    if provider == "twitter" and "no video could be found" in detail:
        return "This X post contains no downloadable video"
    if provider == "instagram" and "no video formats found" in detail:
        return "This Instagram post has no publicly extractable video"
    if "drm protected" in detail:
        return "This source does not provide public downloadable audio"
    if "unsupported url" in detail:
        return "This type of source link is not supported"
    if "http error 403" in detail or "forbidden" in detail:
        return "This source denied anonymous access"
    if any(x in detail for x in ("not available", "video unavailable", "deleted", "removed")):
        return "This public item is unavailable or has been removed"
    return "Source extractor could not access this public item"


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

    async def _progress(self, jid: str, chat: int, stage: str, active: bool = True) -> None:
        from .bot import kb

        message_id = self.store.progress_message(jid)
        label = f"MIKA NEXUS · {stage}\nJob: <code>{jid}</code>"
        keyboard = kb([[("Cancel", f"j:{jid}:cancel")]]) if active else None
        try:
            if message_id is None:
                message = await self.bot.send_message(chat, label, reply_markup=keyboard)
                self.store.bind_progress(jid, message.message_id)
            else:
                await self.bot.edit_message_text(
                    label, chat_id=chat, message_id=message_id, reply_markup=keyboard
                )
        except Exception:
            # Progress messages are advisory; media delivery must continue.
            pass

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
            await self._progress(jid, chat, "Resolving")
            url = await self._resolve_short(url)
            mode = self.store.job_mode(jid)
            if mode == "auto" and (
                source(url) == "soundcloud" or urlsplit(url).hostname == "music.youtube.com"
            ):
                mode = "audio"
            key = hashlib.sha256((url + "|" + mode + "|public").encode()).hexdigest()
            cached = self.store.db.execute(
                "SELECT file_id FROM cache WHERE key=? AND owner_id=?", (key, row["user_id"])
            ).fetchone()
            if cached:
                self.store.transition(jid, "RESOLVING", "DELIVERING")
                try:
                    send = self.bot.send_audio if mode == "audio" else self.bot.send_document
                    sent = await send(
                        chat, cached["file_id"], caption="MIKA NEXUS · " + html.escape(url[:900])
                    )
                except Exception as exc:
                    self.store.transition(
                        jid, "DELIVERING", "DELIVERY_UNCERTAIN", error=type(exc).__name__
                    )
                    return
                self.store.receipt(
                    jid,
                    sent.message_id,
                    sent.audio.file_id
                    if sent.audio
                    else sent.document.file_id
                    if sent.document
                    else None,
                )
                self.store.transition(jid, "DELIVERING", "COMPLETED", receipt_id=sent.message_id)
                await self._progress(jid, chat, "Completed", active=False)
                return
            if mode != "audio" and await self._catalog_matches(jid, url, chat, row["user_id"]):
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
                await self._progress(jid, chat, "Collection ready", active=False)
                return
            self.store.transition(jid, "RESOLVING", "DOWNLOADING")
            await self._progress(jid, chat, "Downloading")
            host = (urlsplit(url).hostname or "").lower()
            if source(url) == "mega":
                raise ValueError("Public MEGA downloads need a supported adapter")
            if source(url) in {"spotify", "deezer"} or host == "music.apple.com":
                raise ValueError("This catalog link does not expose a downloadable full-audio file")
            if source(url) == "direct":
                try:
                    paths = [await self._direct(url, job_dir, max_bytes)]
                except ValueError as exc:
                    if "web page" not in str(exc):
                        raise
                    paths = await self._ytdlp(url, job_dir, max_bytes, mode)
                if mode == "audio" and paths[0].suffix.lower() != ".mp3":
                    paths = [await self._transcode_mp3(paths[0], max_bytes)]
            else:
                prefer_gallery = (source(url) == "instagram" and "/p/" in urlsplit(url).path) or (
                    source(url) == "twitter" and "/status/" in urlsplit(url).path
                )
                paths = None
                if prefer_gallery and mode != "audio":
                    try:
                        paths = await self._gallery_public(url, job_dir, max_bytes)
                    except (ValueError, TimeoutError):
                        paths = None
                    if paths is None:
                        for artifact in job_dir.iterdir():
                            if artifact.is_file():
                                artifact.unlink()
                try:
                    if paths is None:
                        paths = await self._ytdlp(url, job_dir, max_bytes, mode)
                except ValueError as extraction_error:
                    for artifact in job_dir.iterdir():
                        if artifact.is_file():
                            artifact.unlink()
                    photos = None
                    if source(url) == "tiktok":
                        try:
                            if mode != "audio":
                                photos = await self._gallery_tiktok(url, job_dir, max_bytes)
                            if photos is None:
                                photos = await self._gallery_public(url, job_dir, max_bytes)
                        except (ValueError, httpx.HTTPError):
                            photos = None
                        if photos is None:
                            for artifact in job_dir.iterdir():
                                if artifact.is_file():
                                    artifact.unlink()
                            photos = await self._tikwm(url, job_dir, max_bytes)
                        if photos and mode == "audio":
                            if len(photos) != 1:
                                raise ValueError("Cannot extract one MP3 from multiple media items")
                            photos = [await self._transcode_mp3(photos[0], max_bytes)]
                    elif (
                        source(url) in {"instagram", "twitter", "reddit", "pinterest"}
                        and mode != "audio"
                    ):
                        photos = await self._gallery_public(url, job_dir, max_bytes)
                    if photos is None:
                        raise extraction_error
                    paths = photos
            if paths is None:
                raise ValueError("no public media found")
            current_job = self.store.job(jid)
            if current_job and current_job["state"] == "CANCELLED":
                return
            self.store.transition(jid, "DOWNLOADING", "VERIFYING", artifact=str(job_dir))
            await self._progress(jid, chat, "Processing")
            if sum(p.stat().st_size for p in paths) > max_bytes:
                raise ValueError("gallery exceeds Bot API mode limit")
            checks = [await verify(path, max_bytes) for path in paths]
            if not self.store.transition(jid, "VERIFYING", "DELIVERING"):
                return
            await self._progress(jid, chat, "Uploading")
            try:
                sent_messages = []
                for chunk in groups(paths):
                    if len(chunk) == 1:
                        send = (
                            self.bot.send_audio
                            if chunk[0].suffix.lower() == ".mp3"
                            else self.bot.send_document
                        )
                        sent_messages.append(
                            await send(
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
            await self._progress(jid, chat, "Completed", active=False)
            if len(paths) == 1 and sent_messages[0].document:
                self.store.db.execute(
                    "INSERT OR REPLACE INTO cache VALUES (?,?,?,?,strftime('%s','now'))",
                    (key, sent_messages[0].document.file_id, "document", row["user_id"]),
                )
            elif len(paths) == 1 and sent_messages[0].audio:
                self.store.db.execute(
                    "INSERT OR REPLACE INTO cache VALUES (?,?,?,?,strftime('%s','now'))",
                    (key, sent_messages[0].audio.file_id, "audio", row["user_id"]),
                )
            if mode != "audio" and any(x["audio"] for x in checks):
                from .bot import kb

                await self.bot.send_message(
                    chat,
                    "Extract this post's audio as MP3?",
                    reply_markup=kb([[("🎵 MP3", f"j:{jid}:audio")]]),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            current = self.store.job(jid)
            if current and current["state"] not in {"CANCELLED", "DELIVERY_UNCERTAIN"}:
                safe = (
                    str(exc)[:180]
                    if isinstance(exc, ValueError)
                    else "Source network temporarily unavailable"
                    if isinstance(exc, httpx.HTTPError)
                    else type(exc).__name__
                )
                self.store.transition(jid, current["state"], "FAILED", error=safe)
                await self._progress(jid, chat, "Failed", active=False)
                await self.bot.send_message(chat, f"MIKA NEXUS · Could not process link: {safe}")
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    async def _browse(self, url: str) -> tuple[list[dict[str, str]], str]:
        if urlsplit(url).hostname == "podcasts.apple.com":
            return await episodes(await apple_podcast_feed(url))
        if is_feed_url(url):
            return await episodes(url)
        catalog = await resolve_collection(url)
        if catalog is not None:
            return catalog
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
            if source(url) in {"instagram", "tiktok", "twitter"}:
                return await self._browse_profile_gallery(url)
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
        if not items and source(url) in {"instagram", "tiktok", "twitter"}:
            return await self._browse_profile_gallery(url)
        return items[:30], str(data.get("title") or "Collection")

    async def _resolve_short(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        path = urlsplit(url).path
        if not (
            host in {"t.co", "pin.it", "fb.watch", "vm.tiktok.com", "vt.tiktok.com"}
            or source(url) == "tiktok"
            and path.startswith("/t/")
        ):
            return url
        current = url
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            for _ in range(5):
                validate_dns(current)
                for attempt in range(2):
                    try:
                        async with client.stream("GET", current) as response:
                            if response.status_code not in {301, 302, 303, 307, 308}:
                                response.raise_for_status()
                                return current
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError("short link has no redirect destination")
                            current = clean_url(urljoin(current, location))
                            break
                    except httpx.RequestError:
                        if attempt:
                            raise ValueError("Short-link source temporarily unavailable") from None
                        await asyncio.sleep(1)
        raise ValueError("too many short-link redirects")

    async def _browse_profile_gallery(self, url: str) -> tuple[list[dict[str, str]], str]:
        """Use a bounded metadata listing for public social profiles."""
        validate_dns(url)
        if not shutil.which("gallery-dl"):
            raise ValueError("public profile browser unavailable")
        proc = await asyncio.create_subprocess_exec(
            "gallery-dl",
            "-J",
            "--range",
            "1-30",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env={"PATH": os.environ.get("PATH", "")},
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), 45)
        except (TimeoutError, asyncio.CancelledError) as exc:
            import signal

            os.killpg(proc.pid, signal.SIGTERM)
            await proc.wait()
            if isinstance(exc, TimeoutError):
                raise ValueError(
                    "Public gallery inspection timed out; source may require login"
                ) from None
            raise
        if proc.returncode:
            raise ValueError("public profile information unavailable or requires login")
        try:
            messages = json.loads(stdout)
        except ValueError:
            raise ValueError("public profile information unavailable") from None
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        provider = source(url)
        username = urlsplit(url).path.strip("/").split("/")[0]
        for message in messages:
            if (
                not isinstance(message, list)
                or len(message) < 3
                or not isinstance(message[-1], dict)
            ):
                continue
            meta = message[-1]
            target = ""
            if provider == "instagram" and meta.get("post_shortcode"):
                target = f"https://www.instagram.com/p/{meta['post_shortcode']}/"
            elif provider == "twitter" and meta.get("tweet_id"):
                target = f"https://x.com/{username}/status/{meta['tweet_id']}"
            elif provider == "tiktok" and meta.get("id") and meta.get("type") != "avatar":
                target = f"https://www.tiktok.com/{username}/video/{meta['id']}"
            elif provider == "tiktok" and meta.get("type") == "avatar" and message[0] == 3:
                target = str(message[1])
            try:
                target = clean_url(target)
            except ValueError:
                continue
            if target in seen:
                continue
            seen.add(target)
            title = str(meta.get("description") or meta.get("title") or "Post")[:100]
            items.append({"title": title, "url": target})
            if len(items) >= 30:
                break
        if not items:
            raise ValueError("no public profile posts found or source requires login")
        return items, f"Profile · {username}"

    async def _catalog_matches(self, jid: str, url: str, chat: int, user: int) -> bool:
        if source(url) not in {"spotify", "deezer", "apple"}:
            return False
        track = await resolve_track(url)
        if track is None:
            return False
        hits, _ = await Searcher().search(
            f"{track.artist} {track.title}".strip(),
            {"track"},
            {"soundcloud", "ytmusic"},
        )
        ranked = sorted(
            (
                (
                    match_score(
                        track.title, track.artist, h.title, h.creator, track.duration, h.duration
                    ),
                    h,
                )
                for h in hits
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        items = [
            {"title": f"{h.title} — {h.creator}"[:100], "url": h.url}
            for score, h in ranked
            if score >= 0.48
        ][:30]
        if not items:
            raise ValueError(
                "No confident public audio match found; try /music with the track name"
            )
        cid = self.store.create_collection(user, chat, f"Audio matches · {track.title}", items)
        from .bot import kb

        buttons = [
            [(f"{i + 1}. {item['title'][:45]}", f"v:{cid}:d:{i}")]
            for i, item in enumerate(items[:10])
        ]
        if len(items) > 10:
            buttons.append([("Next ▶", f"v:{cid}:p:1")])
        sent = await self.bot.send_message(
            chat,
            f"MIKA NEXUS · {html.escape(track.title)} — {html.escape(track.artist)}\n"
            "Choose a public recording to download:",
            reply_markup=kb(buttons),
        )
        self.store.db.execute(
            "UPDATE collections SET message_id=? WHERE id=?", (sent.message_id, cid)
        )
        self.store.transition(jid, "RESOLVING", "COMPLETED", receipt_id=sent.message_id)
        await self._progress(jid, chat, "Matches ready", active=False)
        return True

    async def _direct(
        self, url: str, directory: Path, max_bytes: int, stem: str = "artifact"
    ) -> Path:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for _ in range(5):
                validate_dns(url)
                async with client.stream("GET", url) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        target = resp.headers.get("location")
                        if not target:
                            raise ValueError("redirect without destination")
                        url = urljoin(url, target)
                        continue
                    resp.raise_for_status()
                    mime = resp.headers.get("content-type", "").split(";")[0].lower()
                    if mime in {"text/html", "application/xhtml+xml"}:
                        raise ValueError("URL is a web page, not a direct file")
                    suffix = MIME_EXT.get(mime) or Path(urlsplit(url).path).suffix[:10] or ".bin"
                    if not suffix.startswith(".") or not suffix[1:].isalnum():
                        suffix = ".bin"
                    path = directory / (stem + suffix)
                    total = 0
                    with path.open("wb") as f:
                        async for chunk in resp.aiter_bytes(65536):
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError("file exceeds Bot API mode limit")
                            f.write(chunk)
                    return path
        raise ValueError("too many redirects")

    async def _transcode_mp3(self, path: Path, max_bytes: int) -> Path:
        await verify(path, max_bytes)
        target = path.with_suffix(".mp3")
        if target == path:
            return path
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(path),
            "-vn",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            await asyncio.wait_for(proc.communicate(), 180)
        except (TimeoutError, asyncio.CancelledError):
            import signal

            os.killpg(proc.pid, signal.SIGTERM)
            await proc.wait()
            raise
        if proc.returncode or not target.exists() or target.stat().st_size > max_bytes:
            raise ValueError("MP3 audio extraction failed or exceeds the Bot API limit")
        path.unlink()
        return target

    async def _gallery_tiktok(self, url: str, directory: Path, max_bytes: int) -> list[Path] | None:
        if not shutil.which("gallery-dl"):
            raise ValueError("TikTok photo support is not installed")
        target = url
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            for _ in range(5):
                validate_dns(target)
                async with client.stream("GET", target) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("TikTok redirect has no destination")
                        target = clean_url(urljoin(target, location))
                        continue
                    response.raise_for_status()
                    break
            else:
                raise ValueError("too many TikTok redirects")
        if source(target) != "tiktok" or "/photo/" not in urlsplit(target).path:
            return None
        proc = await asyncio.create_subprocess_exec(
            "gallery-dl",
            "-g",
            target,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env={"PATH": os.environ.get("PATH", "")},
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), 45)
        except (TimeoutError, asyncio.CancelledError) as exc:
            import signal

            os.killpg(proc.pid, signal.SIGTERM)
            await proc.wait()
            if isinstance(exc, TimeoutError):
                raise ValueError(
                    "TikTok photo inspection timed out; source may require login"
                ) from None
            raise
        if proc.returncode:
            raise ValueError("TikTok photo extraction unavailable")
        candidates = [line for line in stdout.decode().splitlines() if line.startswith("http")]
        if not candidates:
            raise ValueError("TikTok photo unavailable; source may require login")
        if len(candidates) > 35:
            raise ValueError("TikTok photo collection is too large")
        media_urls = [clean_url(candidate) for candidate in candidates[:10]]
        paths: list[Path] = []
        remaining = max_bytes
        for index, media_url in enumerate(media_urls):
            path = await self._direct(media_url, directory, remaining, stem=f"{index:02d}")
            paths.append(path)
            remaining -= path.stat().st_size
        return paths

    async def _tikwm(self, url: str, directory: Path, max_bytes: int) -> list[Path] | None:
        """Last resort for a public TikTok post when source CDNs deny anonymous access."""
        validate_dns(url)
        match = re.search(r"/(video|photo)/(\d+)", urlsplit(url).path)
        if not match:
            return None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.get("https://www.tikwm.com/api/", params={"url": url})
                    response.raise_for_status()
                    payload = response.json()
                data = payload.get("data") or {}
                if payload.get("code") != 0 or str(data.get("id")) != match[2]:
                    return None
                if match[1] == "photo":
                    candidates = data.get("images") or []
                    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 31:
                        return None
                else:
                    if isinstance(data.get("size"), (int, float)) and data["size"] > max_bytes:
                        raise ValueError("TikTok video exceeds the Bot API limit")
                    candidates = [data.get("play")]
                paths: list[Path] = []
                remaining = max_bytes
                for index, candidate in enumerate(candidates):
                    if not isinstance(candidate, str):
                        return None
                    target = clean_url(urljoin("https://www.tikwm.com", candidate))
                    path = await self._direct(target, directory, remaining, stem=f"{index:02d}")
                    paths.append(path)
                    remaining -= path.stat().st_size
                return paths
            except httpx.HTTPError:
                if attempt:
                    raise ValueError(
                        "TikTok public media service is temporarily unavailable"
                    ) from None
                for artifact in directory.iterdir():
                    if artifact.is_file():
                        artifact.unlink()
                await asyncio.sleep(1)
        return None

    async def _gallery_public(self, url: str, directory: Path, max_bytes: int) -> list[Path] | None:
        """Fallback for public image/carousel posts absent from yt-dlp."""
        validate_dns(url)
        if not shutil.which("gallery-dl"):
            return None
        proc = await asyncio.create_subprocess_exec(
            "gallery-dl",
            "-g",
            "--range",
            "1-31",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env={"PATH": os.environ.get("PATH", "")},
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), 45)
        except (TimeoutError, asyncio.CancelledError) as exc:
            import signal

            os.killpg(proc.pid, signal.SIGTERM)
            await proc.wait()
            if isinstance(exc, TimeoutError):
                raise ValueError(
                    "Public gallery inspection timed out; source may require login"
                ) from None
            raise
        if proc.returncode:
            return None
        urls = [
            line
            for line in stdout.decode(errors="replace").splitlines()
            if line.startswith(("https://", "http://"))
        ]
        if not urls or len(urls) > 31:
            return None
        paths: list[Path] = []
        remaining = max_bytes
        for index, item in enumerate(urls):
            path = await self._direct(clean_url(item), directory, remaining, stem=f"{index:02d}")
            paths.append(path)
            remaining -= path.stat().st_size
        return paths

    async def _ytdlp(
        self, url: str, directory: Path, max_bytes: int, mode: str = "auto"
    ) -> list[Path]:
        validate_dns(url)
        if not shutil.which("yt-dlp"):
            raise ValueError("yt-dlp is not installed")
        template = str(directory / "%(autonumber)02d.%(ext)s")
        audio_args = (
            ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]
            if mode == "audio"
            else []
        )
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
            *audio_args,
            url,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env={"PATH": os.environ.get("PATH", "")},
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), 600)
        except (TimeoutError, asyncio.CancelledError) as exc:
            import signal

            os.killpg(proc.pid, signal.SIGTERM)
            await proc.wait()
            if isinstance(exc, TimeoutError):
                raise ValueError("Source download timed out") from None
            raise
        if proc.returncode:
            raise ValueError(extractor_error(url, stderr))
        files = [p for p in directory.iterdir() if p.is_file() and not p.name.endswith(".part")]
        if not 1 <= len(files) <= 10:
            raise ValueError("source returned no bounded deliverable media")
        return sorted(files)
