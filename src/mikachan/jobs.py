"""Bounded on-demand downloader and Telegram delivery."""

import asyncio
import hashlib
import html
import json
import os
import re
import shutil
import sys
from functools import partial
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from .config import Settings
from .security import (source, validate_dns, collection, clean_url, extractor_env,
                       instagram_cookie_args)
from .store import Store
from .media import gallery_budget, groups, media_kind, verify
from .catalog import _Meta, recording_search_alias, resolve_collection, resolve_track
from .matching import match_score
from .search import Searcher
from .podcasts import apple_podcast_feed, episodes, is_feed_url, resolve_episode_link
from .public_social import public_tiktok_sound, public_x_post

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


def gallery_media_headers(meta: dict[str, object]) -> dict[str, str]:
    supplied = meta.get("_http_headers")
    headers: dict[str, str] = {}
    if isinstance(supplied, dict):
        for name in ("User-Agent", "Referer"):
            value = supplied.get(name)
            if isinstance(value, str) and len(value) <= 200 and "\n" not in value:
                if name != "Referer" or value.startswith("https://www.instagram.com/"):
                    headers[name] = value
    return headers


def invalid_cached_file_id(exc: TelegramBadRequest) -> bool:
    """Only a definite file-ID rejection permits a fresh download and resend."""
    detail = str(exc).lower()
    return any(text in detail for text in (
        "wrong file identifier", "file_id is invalid", "wrong remote file identifier",
    ))


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
    if any(x in detail for x in ("not available", "video unavailable", "deleted", "removed",
                                "disabled")):
        return "This public item is unavailable or has been removed"
    return "Source extractor could not access this public item"


class Worker:
    def __init__(self, settings: Settings, store: Store, bot: Bot):
        self.settings, self.store, self.bot = settings, store, bot
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.running_users: dict[int, int] = {}
        self.running = False

    async def loop(self) -> None:
        self.running = True
        while self.running:
            while len(self.tasks) < self.settings.max_active_downloads:
                row = next((candidate for candidate in self.store.next_jobs()
                            if self.running_users.get(int(candidate['user_id']), 0)
                            < self.settings.max_active_per_user), None)
                if row is None:
                    break
                if not self.store.transition(row['id'], row['state'], 'RESOLVING'):
                    break
                user = int(row['user_id'])
                self.running_users[user] = self.running_users.get(user, 0) + 1
                task = asyncio.create_task(self._run(row['id']))
                self.tasks[row['id']] = task
                task.add_done_callback(partial(self._task_done, str(row['id']), user))
            await asyncio.sleep(0.5)

    def _done(self, jid: str, user: int) -> None:
        self.tasks.pop(jid, None)
        remaining = self.running_users.get(user, 0) - 1
        if remaining > 0:
            self.running_users[user] = remaining
        else:
            self.running_users.pop(user, None)

    def _task_done(self, jid: str, user: int, task: asyncio.Task[None]) -> None:
        self._done(jid, user)
        if not task.cancelled():
            task.exception()

    async def stop(self) -> None:
        self.running = False
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

    async def _progress(self, jid: str, chat: int, stage: str, active: bool = True) -> None:
        from .bot import kb

        message_id = self.store.progress_message(jid)
        labels = {
            "Resolving": "🔎 Checking the link…",
            "Downloading": "⬇️ Downloading media…",
            "Processing": "🎛 Preparing media…",
            "Uploading": "📤 Sending to Telegram…",
        }
        label = labels.get(stage, stage)
        keyboard = kb([[("Cancel ✕", f"j:{jid}:cancel")]]) if active else None
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

    async def _clear_progress(self, jid: str, chat: int) -> None:
        message_id = self.store.progress_message(jid)
        if message_id is not None:
            try:
                await self.bot.delete_message(chat, message_id)
            except Exception:
                pass

    @staticmethod
    def _extract_meta(directory: Path) -> dict[str, object]:
        meta: dict[str, object] = {}
        sidecar = directory / ".mikachan-meta.json"
        if not sidecar.exists():
            sidecar = directory / (".nex" + "us-meta.json")
        files = [sidecar] if sidecar.exists() else sorted(directory.glob("*.info.json"))
        if files:
            try:
                value = json.loads(files[0].read_text(errors="replace"))
                if isinstance(value, dict):
                    meta = value
            except (OSError, ValueError):
                pass
        return meta

    @staticmethod
    def _caption(
        url: str, directory: Path, context: dict[str, object] | None = None,
        kind: str = "video",
    ) -> tuple[str, str]:
        """Show concise text on the media; retain full detail for an optional button."""
        meta = Worker._extract_meta(directory)
        context = context or {}
        raw = next(
            (
                str(context.get(key) or meta[key]).strip()
                for key in ("description", "content", "caption", "title")
                if isinstance(context.get(key) or meta.get(key), str)
                and str(context.get(key) or meta.get(key)).strip()
            ),
            "",
        )
        creator = next(
            (
                str(context.get(key) or meta[key]).strip()
                for key in ("artist", "uploader", "nickname", "channel")
                if isinstance(context.get(key) or meta.get(key), str)
                and str(context.get(key) or meta.get(key)).strip()
            ),
            "",
        )
        if kind == "audio":
            title = str(context.get("title") or meta.get("track") or meta.get("title") or "").strip()
            summary = "\n".join(x for x in (f"🎵 {title[:160]}" if title else "",
                                          f"👤 {creator[:100]}" if creator else "") if x)
            return html.escape(summary), raw
        # YouTube music descriptions often contain pages of autogenerated credits.
        budget = 280 if source(url) == "youtube" else 900
        summary = (f"👤 {creator[:80]}\n" if creator else "") + raw[:budget]
        return html.escape(summary.strip()), raw[budget:]

    @staticmethod
    def _buttons(jid: str, url: str, audio: bool, details: bool, lyrics: bool,
                 transcript: bool = False,
                 context: dict[str, object] | None = None) -> InlineKeyboardMarkup:
        row = [InlineKeyboardButton(text="🔗 Link", url=url)]
        if audio:
            row.append(InlineKeyboardButton(text="🎵 MP3", callback_data=f"j:{jid}:audio"))
        rows = [row]
        extras = []
        if lyrics:
            extras.append(InlineKeyboardButton(text="📝 Lyrics", callback_data=f"j:{jid}:lyrics"))
        if details:
            extras.append(InlineKeyboardButton(text="ℹ️ Details", callback_data=f"j:{jid}:info"))
        if transcript:
            extras.append(InlineKeyboardButton(text="📄 Transcript", callback_data=f"j:{jid}:transcribe"))
        if extras:
            rows.append(extras)
        from .follow import SOCIAL_PLATFORMS
        if source(url) in SOCIAL_PLATFORMS:
            rows.append([InlineKeyboardButton(text="👤 Profile",
                                            callback_data=f"j:{jid}:profile")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def _save_meta(directory: Path, data: dict[str, object]) -> None:
        (directory / ".mikachan-meta.json").write_text(json.dumps(data, ensure_ascii=False))

    async def _deliver_text_post(
        self, jid: str, chat: int, url: str, context: dict[str, object], body: str,
    ) -> None:
        """Deliver an X status that has text but no file to download."""
        if not self.store.transition(jid, "RESOLVING", "DELIVERING"):
            return
        context.update({"source_url": url, "description": body, "kind": "post"})
        self.store.set_job_context(jid, context)
        handle = urlsplit(url).path.strip("/").split("/")[0]
        content = f"👤 @{handle}\n\n{body}"
        chunks = [content[i:i + 3900] for i in range(0, len(content), 3900)]
        markup = self._buttons(jid, url, False, False, False, context=context)
        try:
            first = None
            for index, chunk in enumerate(chunks):
                sent = await self.bot.send_message(
                    chat, chunk, parse_mode=None,
                    reply_markup=markup if index == 0 else None,
                )
                first = first or sent
        except Exception as exc:
            self.store.transition(jid, "DELIVERING", "DELIVERY_UNCERTAIN",
                                  error=type(exc).__name__)
            return
        self.store.receipt(jid, first.message_id, None)
        self.store.transition(jid, "DELIVERING", "COMPLETED", receipt_id=first.message_id)
        await self._clear_progress(jid, chat)

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
            original_url = url
            context = self.store.job_context(jid)
            mode = self.store.job_mode(jid)
            if mode == "auto" and context.get("kind") in {"track", "episode"}:
                mode = "audio"
            if mode == "auto" and (
                source(url) == "soundcloud" or urlsplit(url).hostname == "music.youtube.com"
            ):
                mode = "audio"
            sound_match = (re.search(r"/music/(?:[^/]*-)?(\d{10,22})/?$", urlsplit(url).path)
                           if source(url) == "tiktok" else None)
            if sound_match:
                mode = "audio"
            catalog_track = await resolve_track(url) if source(url) in {
                "spotify", "deezer", "apple"
            } else None
            if catalog_track is not None:
                mode = "audio"
                context.update({
                    "title": catalog_track.title, "artist": catalog_track.artist,
                    "duration": catalog_track.duration or 0, "cover": catalog_track.cover,
                    "kind": "track", "source_url": original_url,
                })
                self.store.set_job_context(jid, context)
            episode = await resolve_episode_link(url)
            if episode:
                mode = "audio"
                context.update({k: v for k, v in episode.items() if k != "url"})
                self.store.set_job_context(jid, context)
                url = str(episode["url"])
            key = hashlib.sha256((original_url + "|" + mode + "|public").encode()).hexdigest()
            cached = self.store.db.execute(
                "SELECT file_id,media_kind FROM cache WHERE key=? AND owner_id=?",
                (key, row["user_id"]),
            ).fetchone()
            if cached:
                self.store.transition(jid, "RESOLVING", "DELIVERING")
                try:
                    stored_caption = self.store.db.execute(
                        "SELECT caption FROM cache_captions WHERE key=?", (key,)
                    ).fetchone()
                    stored_audio = self.store.db.execute(
                        "SELECT has_audio FROM cache_audio WHERE key=?", (key,)
                    ).fetchone()
                    stored_kind = str(cached["media_kind"])
                    source_link = clean_url(str(context.get("source_url") or original_url))
                    markup = self._buttons(
                        jid, source_link,
                        bool(mode != "audio" and stored_audio and stored_audio[0]),
                        bool(context.get("description")),
                        stored_kind == "audio" and context.get("kind") == "track",
                        context.get("kind") == "episode",
                        context,
                    )
                    value = stored_caption[0] if stored_caption else None
                    if stored_kind == "audio":
                        sent = await self.bot.send_audio(
                            chat, cached["file_id"], caption=value, reply_markup=markup,
                            title=str(context.get("title") or "Audio")[:64],
                            performer=str(context.get("artist") or "")[:64] or None,
                        )
                    elif stored_kind == "video":
                        sent = await self.bot.send_video(
                            chat, cached["file_id"], caption=value, reply_markup=markup,
                            supports_streaming=True,
                        )
                    elif stored_kind == "photo":
                        sent = await self.bot.send_photo(
                            chat, cached["file_id"], caption=value, reply_markup=markup,
                        )
                    else:
                        sent = await self.bot.send_document(
                            chat, cached["file_id"], caption=value, reply_markup=markup,
                        )
                except TelegramBadRequest as exc:
                    if invalid_cached_file_id(exc):
                        self.store.db.execute("DELETE FROM cache WHERE key=?", (key,))
                        self.store.db.execute("DELETE FROM cache_captions WHERE key=?", (key,))
                        self.store.db.execute("DELETE FROM cache_audio WHERE key=?", (key,))
                        self.store.transition(jid, "DELIVERING", "RESOLVING")
                    else:
                        self.store.transition(
                            jid, "DELIVERING", "DELIVERY_UNCERTAIN", error=type(exc).__name__
                        )
                        await self._progress(
                            jid, chat, "⚠️ Telegram did not confirm delivery. Check the chat before retrying.",
                            active=False,
                        )
                        return
                except Exception as exc:
                    self.store.transition(
                        jid, "DELIVERING", "DELIVERY_UNCERTAIN", error=type(exc).__name__
                    )
                    await self._progress(
                        jid, chat, "⚠️ Telegram did not confirm delivery. Check the chat before retrying.",
                        active=False,
                    )
                    return
                else:
                    self.store.receipt(
                        jid,
                        sent.message_id,
                        sent.audio.file_id if sent.audio else
                        sent.video.file_id if sent.video else
                        sent.photo[-1].file_id if sent.photo else
                        sent.document.file_id if sent.document else None,
                    )
                    self.store.transition(jid, "DELIVERING", "COMPLETED", receipt_id=sent.message_id)
                    await self._clear_progress(jid, chat)
                    return
            x_post = None
            if source(url) == "twitter" and "/status/" in urlsplit(url).path \
                    and mode != "audio":
                try:
                    x_post = await public_x_post(url)
                except (ValueError, TimeoutError, httpx.HTTPError):
                    pass
                if x_post and x_post[0].strip() and not x_post[1]:
                    await self._deliver_text_post(jid, chat, original_url, context, x_post[0])
                    return
            if await self._profile(jid, original_url, chat, job_dir):
                return
            if collection(url):
                items, title = await self._browse(url)
                if not items:
                    raise ValueError("No public collection items found")
                cid = self.store.create_collection(
                    row["user_id"], chat, title, items,
                    {"source_url": url, "kind": source(url),
                     "owner_url": items[0].get("owner_url", "")},
                )
                from .bot import collection_view

                caption, markup = collection_view(
                    cid, title, items, 0, self.store.collection_context(cid)
                )
                sent = await self.bot.send_message(chat, caption, reply_markup=markup)
                self.store.db.execute(
                    "UPDATE collections SET message_id=? WHERE id=?", (sent.message_id, cid)
                )
                self.store.transition(jid, "RESOLVING", "COMPLETED", receipt_id=sent.message_id)
                await self._clear_progress(jid, chat)
                return
            self.store.transition(jid, "RESOLVING", "DOWNLOADING")
            await self._progress(jid, chat, "Downloading")
            host = (urlsplit(url).hostname or "").lower()
            if sound_match:
                audio_url, sound_title, sound_duration = await public_tiktok_sound(sound_match[1])
                context.update({"kind": "track", "title": sound_title,
                                "duration": sound_duration, "source_url": original_url})
                self.store.set_job_context(jid, context)
                mode = "audio"
                paths = [await self._direct(audio_url, job_dir, max_bytes)]
            elif catalog_track is not None:
                paths = await self._catalog_audio(catalog_track, job_dir, max_bytes)
            elif source(url) == "mega":
                raise ValueError("Public MEGA downloads need a supported adapter")
            elif source(url) in {"spotify", "deezer"} or host == "music.apple.com":
                raise ValueError("This catalog link does not expose a downloadable full-audio file")
            elif source(url) == "danbooru":
                try:
                    paths = [await self._danbooru_post(url, job_dir, max_bytes)]
                except (ValueError, TimeoutError, httpx.HTTPError):
                    paths = await self._gallery_public(url, job_dir, max_bytes)
                    if not paths:
                        raise ValueError("This Danbooru post has no accessible public media") from None
            elif source(url) == "direct":
                input_limit = 300 * 1024 * 1024 if context.get("kind") == "episode" else max_bytes
                try:
                    paths = [await self._direct(url, job_dir, input_limit)]
                except ValueError as exc:
                    if "web page" not in str(exc):
                        raise
                    paths = await self._ytdlp(
                        url, job_dir,
                        min(300 * 1024 * 1024, max_bytes * 6) if mode == "audio" else max_bytes,
                        mode)
                if mode == "audio":
                    if paths[0].suffix.lower() != ".mp3":
                        paths = [
                            await (
                                self._compress_mp3(paths[0], max_bytes)
                                if paths[0].stat().st_size > max_bytes
                                else self._transcode_mp3(paths[0], max_bytes)
                            )
                        ]
                    elif paths[0].stat().st_size > max_bytes:
                        paths = [await self._compress_mp3(paths[0], max_bytes)]
            else:
                instagram_gallery = source(url) == "instagram" and any(
                    marker in urlsplit(url).path for marker in ("/p/", "/stories/")
                )
                prefer_gallery = instagram_gallery or (
                    source(url) == "twitter" and "/status/" in urlsplit(url).path
                )
                paths = None
                if source(url) == "instagram" and mode == "audio":
                    try:
                        paths = await self._gallery_public(
                            url, job_dir, max_bytes, audio_only=True)
                        if paths:
                            paths = [await self._transcode_mp3(paths[0], max_bytes)]
                    except (ValueError, TimeoutError, httpx.HTTPError):
                        paths = None
                        for artifact in job_dir.iterdir():
                            if artifact.is_file():
                                artifact.unlink()
                if source(url) == "twitter" and "/status/" in urlsplit(url).path \
                        and mode != "audio":
                    try:
                        media_urls = x_post[1] if x_post else None
                        if media_urls:
                            remaining = gallery_budget(max_bytes)
                            paths = []
                            for index, media_url in enumerate(media_urls):
                                path = await self._direct(media_url, job_dir,
                                                          min(max_bytes, remaining),
                                                          stem=f"{index:02d}")
                                paths.append(path)
                                remaining -= path.stat().st_size
                    except (ValueError, TimeoutError, httpx.HTTPError):
                        paths = None
                        for artifact in job_dir.iterdir():
                            if artifact.is_file():
                                artifact.unlink()
                if prefer_gallery and mode != "audio":
                    if paths is None:
                        try:
                            paths = await self._gallery_public(url, job_dir, max_bytes)
                        except (ValueError, TimeoutError, httpx.HTTPError):
                            paths = None
                        if paths is None:
                            for artifact in job_dir.iterdir():
                                if artifact.is_file():
                                    artifact.unlink()
                try:
                    if paths is None:
                        download_limit = (
                            min(240 * 1024 * 1024, max_bytes * 5)
                            if mode == "audio" or source(url) == "youtube" and mode != "document"
                            else max_bytes
                        )
                        paths = await self._ytdlp(url, job_dir, download_limit, mode)
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
                        try:
                            photos = await self._gallery_public(url, job_dir, max_bytes)
                        except (ValueError, TimeoutError, httpx.HTTPError):
                            photos = None
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
            if mode == "audio" and len(paths) == 1 and paths[0].stat().st_size > max_bytes:
                paths = [await self._compress_mp3(paths[0], max_bytes)]
            if source(original_url) == "youtube" and mode != "audio" and len(paths) == 1 \
                    and paths[0].stat().st_size > max_bytes:
                paths = [await self._fit_video(paths[0], max_bytes, mode)]
            if sum(p.stat().st_size for p in paths) > gallery_budget(max_bytes):
                raise ValueError("gallery exceeds Bot API mode limit")
            if mode != "document":
                paths = await self._prepare_media(paths, max_bytes)
            checks = [await verify(path, max_bytes) for path in paths]
            kinds = [media_kind(path, check) for path, check in zip(paths, checks)]
            if mode == "document":
                kinds = ["document" for _ in kinds]
            meta = self._extract_meta(job_dir)
            if context.get("provisional_title") and meta.get("title"):
                context["title"] = str(meta["title"])
            for field, names in {
                "title": ("audio_title", "track", "title"),
                "artist": ("audio_artist", "artist", "uploader", "channel"),
                "description": ("description", "content", "caption"),
                "cover": ("thumbnail",),
                "uploader_url": ("uploader_url", "channel_url"),
                "uploader_id": ("uploader_id", "channel_id"),
                "username": ("username",),
            }.items():
                if not context.get(field):
                    context[field] = next(
                        (str(meta[name])[:12000] for name in names
                         if isinstance(meta.get(name), str) and meta[name]), ""
                    )
            context.setdefault("source_url", original_url)
            if not context.get("kind"):
                context["kind"] = "track" if kinds == ["audio"] else "post"
            self.store.set_job_context(jid, context)
            first_kind = "audio" if kinds == ["audio"] else "video"
            caption, _ = self._caption(original_url, job_dir, context, first_kind)
            source_link = clean_url(str(context.get("source_url") or original_url))
            cover = await self._cover(str(context.get("cover") or ""), job_dir) if "audio" in kinds else None
            if not self.store.transition(jid, "VERIFYING", "DELIVERING"):
                return
            await self._progress(jid, chat, "Uploading")
            try:
                sent_messages: list[Message] = []
                visuals = [(p, k, c) for p, k, c in zip(paths, kinds, checks) if k in {"photo", "video"}]
                others = [(p, k, c) for p, k, c in zip(paths, kinds, checks) if k not in {"photo", "video"}]
                for chunk in groups([p for p, _, _ in visuals]):
                    if len(chunk) == 1:
                        path = chunk[0]
                        check = next(c for p, _, c in visuals if p == path)
                        markup = self._buttons(
                            jid, source_link,
                            len(paths) == 1 and mode != "audio" and check["audio"],
                            bool(context.get("description")), False,
                            context=context,
                        )
                        if media_kind(path, check) == "photo":
                            sent = await self.bot.send_photo(
                                chat, FSInputFile(path),
                                caption=caption if not sent_messages else None,
                                reply_markup=markup,
                            )
                        else:
                            try:
                                sent = await self.bot.send_video(
                                    chat, FSInputFile(path), supports_streaming=True,
                                    caption=caption if not sent_messages else None,
                                    reply_markup=markup,
                                )
                            except TelegramBadRequest:
                                # Telegram can reject a playable MP4 as native
                                # video. A document still delivers the exact file.
                                sent = await self.bot.send_document(
                                    chat, FSInputFile(path),
                                    caption=caption if not sent_messages else None,
                                    reply_markup=markup,
                                )
                        sent_messages.append(sent)
                    else:
                        media: list[
                            InputMediaAudio | InputMediaDocument | InputMediaPhoto | InputMediaVideo
                        ] = []
                        for index, path in enumerate(chunk):
                            item_caption = caption if index == 0 and not sent_messages else None
                            kind = next(k for p, k, _ in visuals if p == path)
                            if kind == "photo":
                                media.append(InputMediaPhoto(media=FSInputFile(path), caption=item_caption))
                            else:
                                media.append(InputMediaVideo(media=FSInputFile(path), caption=item_caption))
                        sent_messages.extend(await self.bot.send_media_group(chat, media))
                for path, kind, _ in others:
                    markup = self._buttons(
                        jid, source_link, False, bool(context.get("description")),
                        kind == "audio" and context.get("kind") == "track",
                        context.get("kind") == "episode",
                        context,
                    )
                    if kind == "audio":
                        title = str(context.get("title") or Path(urlsplit(url).path).stem or "Audio")[:64]
                        artist = str(context.get("artist") or "")[:64]
                        audio_name = re.sub(
                            r"[\\/\x00-\x1f:*?\"<>|]", "_",
                            f"{artist} - {title}" if artist else title,
                        ).strip(" ._")[:150] or "Audio"
                        sent = await self.bot.send_audio(
                            chat, FSInputFile(path, filename=audio_name + ".mp3"),
                            title=title, performer=artist or None,
                            thumbnail=FSInputFile(cover) if cover else None,
                            caption=caption if not sent_messages else None,
                            reply_markup=markup,
                        )
                    else:
                        sent = await self.bot.send_document(
                            chat, FSInputFile(path),
                            caption=caption if not sent_messages else None,
                            reply_markup=markup,
                        )
                    sent_messages.append(sent)
                if len(visuals) > 1 and not others:
                    await self.bot.send_message(
                        chat, "🔗 Post link and details",
                        reply_markup=self._buttons(
                            jid, source_link, False, bool(context.get("description")), False,
                            context=context,
                        ),
                    )
                for sent in sent_messages:
                    file_id = (
                        sent.audio.file_id if sent.audio else
                        sent.video.file_id if sent.video else
                        sent.photo[-1].file_id if sent.photo else
                        sent.document.file_id if sent.document else None
                    )
                    self.store.receipt(jid, sent.message_id, file_id)
            except Exception as exc:
                self.store.transition(
                    jid, "DELIVERING", "DELIVERY_UNCERTAIN", error=type(exc).__name__
                )
                await self._progress(
                    jid, chat,
                    "⚠️ Telegram did not confirm delivery. Check the chat before retrying.",
                    active=False,
                )
                return
            self.store.transition(
                jid, "DELIVERING", "COMPLETED", receipt_id=sent_messages[0].message_id
            )
            await self._clear_progress(jid, chat)
            if len(paths) == 1:
                sent = sent_messages[0]
                file_id = (
                    sent.audio.file_id if sent.audio else
                    sent.video.file_id if sent.video else
                    sent.photo[-1].file_id if sent.photo else
                    sent.document.file_id if sent.document else None
                )
                assert file_id is not None
                self.store.db.execute(
                    "INSERT OR REPLACE INTO cache VALUES (?,?,?,?,strftime('%s','now'))",
                    (key, file_id, "document" if sent.document else kinds[0], row["user_id"]),
                )
                self.store.db.execute(
                    "INSERT OR REPLACE INTO cache_captions VALUES (?,?)", (key, caption)
                )
                self.store.db.execute(
                    "INSERT OR REPLACE INTO cache_audio VALUES (?,?)",
                    (key, int(bool(checks[0]["audio"]))),
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
                await self._progress(jid, chat, f"⚠️ Download failed\n{html.escape(safe)}", active=False)
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
            "--ignore-config",
            *instagram_cookie_args(url),
            *([] if source(url) == "soundcloud" else ["--flat-playlist"]),
            "--playlist-end",
            "300",
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env=extractor_env(),
        )
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), 120 if source(url) == "soundcloud" else 30
            )
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
        owner = data.get("channel_url") or data.get("uploader_url") or ""
        if not owner and source(url) == "youtube" and data.get("channel_id"):
            owner = "https://www.youtube.com/channel/" + str(data["channel_id"])
        try:
            owner = clean_url(str(owner))
        except ValueError:
            owner = ""
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
            slug = urlsplit(target).path.strip("/").split("/")[-1].replace("-", " ")
            title = str(entry.get("title") or entry.get("track") or slug or "Untitled")
            artist = str(entry.get("artist") or entry.get("uploader") or "")
            items.append({
                "title": title[:100], "artist": artist[:100], "url": target,
                "source_url": target,
                "owner_url": owner,
                "kind": "track" if source(url) == "soundcloud" else "video",
                "provisional_title": "" if entry.get("title") or entry.get("track") else "1",
            })
        if not items and source(url) in {"instagram", "tiktok", "twitter"}:
            return await self._browse_profile_gallery(url)
        return items[:300], str(data.get("title") or "Collection")

    async def _resolve_short(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        path = urlsplit(url).path
        if not (
            host in {"t.co", "pin.it", "fb.watch", "vm.tiktok.com", "vt.tiktok.com",
                     "on.soundcloud.com"}
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
            "--config-ignore", "--cache-file", ":memory:",
            *instagram_cookie_args(url),
            "-J",
            "--range",
            "1-30",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env=extractor_env(),
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

    async def _profile(self, jid: str, url: str, chat: int, directory: Path) -> bool:
        provider = source(url)
        parts = [part for part in urlsplit(url).path.split("/") if part]
        if provider not in {"instagram", "tiktok"} or len(parts) != 1:
            return False
        if provider == "tiktok" and not parts[0].startswith("@"):
            return False
        if provider == "instagram" and parts[0] in {"stories", "explore", "reels", "accounts"}:
            return False
        username = parts[0].lstrip("@")
        avatar_url = ""
        display = username
        bio = ""
        avatar_target = (
            f"https://www.tiktok.com/@{username}/avatar" if provider == "tiktok"
            else f"https://www.instagram.com/{username}/avatar/"
        )
        if shutil.which("gallery-dl"):
            proc: asyncio.subprocess.Process | None = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "gallery-dl", "--config-ignore", "--cache-file", ":memory:",
                    *instagram_cookie_args(url),
                    "-j", "--range", "1-1", avatar_target,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True, env=extractor_env(),
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), 25)
                messages = json.loads(stdout)
                for message in messages:
                    if isinstance(message, list) and len(message) >= 3 and message[0] == 3:
                        avatar_url = clean_url(str(message[1]))
                        meta = message[2] if isinstance(message[2], dict) else {}
                        user = meta.get("user") if isinstance(meta.get("user"), dict) else meta
                        if isinstance(user, dict):
                            display = str(user.get("nickname") or user.get("full_name") or display)
                            bio = str(user.get("signature") or user.get("biography") or "")
                        break
            except (TimeoutError, ValueError, OSError):
                import signal
                if proc is not None and proc.returncode is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    await proc.wait()
        if not avatar_url:
            validate_dns(url)
            async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
                response = await client.get(url, headers={"User-Agent": "facebookexternalhit/1.1"})
                response.raise_for_status()
            parser = _Meta()
            parser.feed(response.text[:800_000])
            avatar_url = clean_url(parser.data.get("og:image", "")) if parser.data.get("og:image") else ""
            display = parser.data.get("og:title", "").split(" (@")[0] or display
            bio = parser.data.get("og:description", "") or bio
        if not avatar_url:
            raise ValueError("Public profile information is unavailable")
        avatar = await self._direct(avatar_url, directory, 8 * 1024 * 1024)
        avatar = (await self._prepare_media([avatar], 8 * 1024 * 1024))[0]
        context = {
            "kind": "profile", "profile_url": url, "source_url": url,
            "avatar_url": avatar_url, "title": display,
            "artist": username, "description": bio,
        }
        self.store.set_job_context(jid, context)
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Link", url=url),
             InlineKeyboardButton(text="🖼 Original avatar", callback_data=f"j:{jid}:avatar")],
            [InlineKeyboardButton(text="⬇️ Latest post", callback_data=f"j:{jid}:latest"),
             InlineKeyboardButton(text="⭕ Stories", callback_data=f"j:{jid}:stories")],
            [InlineKeyboardButton(text="👀 Follow", callback_data=f"j:{jid}:follow")],
        ])
        if not self.store.transition(jid, "RESOLVING", "DELIVERING"):
            return True
        try:
            sent = await self.bot.send_photo(
                chat, FSInputFile(avatar),
                caption=html.escape(f"👤 {display[:100]}\n@{username}\n\n{bio[:700]}"),
                reply_markup=markup,
            )
        except Exception as exc:
            self.store.transition(
                jid, "DELIVERING", "DELIVERY_UNCERTAIN", error=type(exc).__name__
            )
            return True
        self.store.receipt(jid, sent.message_id, sent.photo[-1].file_id if sent.photo else None)
        self.store.transition(jid, "DELIVERING", "COMPLETED", receipt_id=sent.message_id)
        await self._clear_progress(jid, chat)
        return True

    async def _catalog_audio(
        self, track: object, directory: Path, max_bytes: int,
    ) -> list[Path]:
        """Choose one full public recording with strict identity and length checks."""
        from .catalog import CatalogTrack

        assert isinstance(track, CatalogTrack)
        alias = await recording_search_alias(track)
        core = re.split(r"[（(]", track.title, maxsplit=1)[0].strip(" -")
        search_core = re.sub(r"[^\w\s]+", " ", core.replace("'", "").replace("’", ""))
        query = f"{search_core} {alias[1]}" if alias else f"{track.artist} {track.title}"
        searcher = Searcher()
        queries = [
            (query.strip(), {"soundcloud", "ytmusic"}),
            (f"{alias[0]} {alias[1]}" if alias else search_core,
             {"soundcloud", "ytmusic", "youtube"}),
        ]
        tried: set[str] = set()
        available_hits = False
        unavailable_sources: set[str] = set()
        def score_hit(hit: object) -> float:
            from .search import Hit
            assert isinstance(hit, Hit)
            scores = [match_score(track.title, track.artist, hit.title, hit.creator,
                                  track.duration, hit.duration)]
            if alias:
                scores.append(match_score(alias[0], alias[1], hit.title, hit.creator,
                                          track.duration, hit.duration))
                scores.append(match_score(search_core, alias[1], hit.title, hit.creator,
                                          track.duration, hit.duration))
            return max(scores)
        for text_query, providers in queries:
            hits, failures = await searcher.search(
                text_query, {"track"}, providers,
                ready=lambda candidates: any(
                    score_hit(hit) >= 0.92 and
                    (not track.duration or not hit.duration
                     or abs(track.duration - hit.duration) <= 5)
                    for hit in candidates),
            )
            available_hits |= bool(hits)
            unavailable_sources.update(failures)
            ranked = sorted(hits, key=lambda h: score_hit(h) +
                            (0.03 if h.source == "ytmusic" else 0.0), reverse=True)
            for hit in ranked[:10]:
                score = score_hit(hit)
                if score < 0.84 or hit.url in tried or (
                    track.duration and hit.duration and abs(track.duration - hit.duration) > 10
                ):
                    continue
                tried.add(hit.url)
                try:
                    paths = await asyncio.wait_for(
                        self._ytdlp(clean_url(hit.url), directory,
                                    min(300 * 1024 * 1024, max_bytes * 6), "audio"), 100
                    )
                    if len(paths) != 1:
                        raise ValueError("recording has unexpected files")
                    if track.duration:
                        proc = await asyncio.create_subprocess_exec(
                            "ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(paths[0]),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        stdout, _ = await asyncio.wait_for(proc.communicate(), 20)
                        seconds = float(stdout.strip() or 0)
                        if abs(track.duration - seconds) > 10:
                            raise ValueError("public recording is not the full track")
                    if paths[0].stat().st_size > max_bytes:
                        paths = [await self._compress_mp3(paths[0], max_bytes)]
                    return paths
                except (ValueError, TimeoutError, OSError):
                    for file in directory.iterdir():
                        if file.is_file():
                            file.unlink()
        if not available_hits and {"soundcloud", "ytmusic", "youtube"} <= unavailable_sources:
            raise ValueError("Public audio sources are unreachable from this server")
        raise ValueError("No complete public recording matched this track")

    async def _direct(
        self, url: str, directory: Path, max_bytes: int, stem: str = "artifact",
        headers: dict[str, str] | None = None,
    ) -> Path:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for _ in range(5):
                validate_dns(url)
                async with client.stream("GET", url, headers=headers) as resp:
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

    async def _danbooru_post(self, url: str, directory: Path, max_bytes: int) -> Path:
        """Resolve one public post using Danbooru's post JSON before downloading."""
        match = re.fullmatch(r"/posts/(\d+)/?", urlsplit(url).path)
        if not match:
            raise ValueError("A Danbooru post link must contain its numeric ID")
        post_id = match[1]
        endpoint = f"https://danbooru.donmai.us/posts/{post_id}.json"
        validate_dns(endpoint)
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            response = await client.get(endpoint, headers={"Accept": "application/json"})
            response.raise_for_status()
            if len(response.content) > 1_000_000:
                raise ValueError("Danbooru response is too large")
            post = response.json()
        if not isinstance(post, dict) or str(post.get("id")) != post_id or post.get("is_deleted"):
            raise ValueError("Danbooru post is unavailable")
        media_url = post.get("file_url") or post.get("large_file_url")
        if not isinstance(media_url, str):
            raise ValueError("Danbooru post has no public file")
        media_url = clean_url(urljoin(endpoint, media_url))
        if not media_url.startswith("https://"):
            raise ValueError("Danbooru file must use HTTPS")
        self._save_meta(directory, {
            "title": f"Danbooru post {post_id}",
            "description": str(post.get("tag_string_general") or "")[:900],
        })
        return await self._direct(media_url, directory, max_bytes)

    async def _transcode_mp3(self, path: Path, max_bytes: int) -> Path:
        await verify(path, max(max_bytes, 300 * 1024 * 1024))
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

    async def _compress_mp3(self, path: Path, max_bytes: int) -> Path:
        await verify(path, max(300 * 1024 * 1024, max_bytes))
        probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(probe.communicate(), 20)
        duration = float(stdout.strip() or 0)
        if duration <= 0:
            raise ValueError("podcast duration unavailable")
        bitrate = min(128, max(32, int(max_bytes * 8 * 0.89 / duration / 1000)))
        target = path.with_name(path.stem + ".telegram.mp3")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
            "-vn", "-c:a", "libmp3lame", "-b:a", f"{bitrate}k",
            "-ac", "1" if bitrate < 64 else "2", str(target),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            await asyncio.wait_for(proc.communicate(), 600)
        except (TimeoutError, asyncio.CancelledError):
            import signal
            os.killpg(proc.pid, signal.SIGTERM)
            await proc.wait()
            raise
        if proc.returncode or not target.exists() or target.stat().st_size > max_bytes:
            raise ValueError("episode is too long for Telegram's public upload limit")
        path.unlink()
        return target

    async def _fit_video(self, path: Path, max_bytes: int, mode: str) -> Path:
        """Fit a YouTube video to the selected Bot API upload budget."""
        probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(path), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(probe.communicate(), 20)
        if probe.returncode:
            raise ValueError("Video duration could not be verified")
        try:
            duration = float(json.loads(stdout)["format"]["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Video duration could not be verified") from exc
        if not 0 < duration <= 7200:
            raise ValueError("Video is too long for the public Telegram upload limit")
        video_rate = int(max_bytes * 0.87 * 8 / duration - 112_000)
        if video_rate < 350_000:
            raise ValueError("Video is too long to fit Telegram's upload limit at usable quality")
        target = path.with_name(path.stem + ".fitted.mp4")
        height = 360 if mode == "360" else 480 if mode == "480" else 720 if mode == "720" else 1080
        if video_rate < 1_200_000:
            height = min(height, 480)
        elif video_rate < 2_500_000:
            height = min(height, 720)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
            "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
            "-preset", "veryfast", "-b:v", str(video_rate),
            "-maxrate", str(video_rate), "-bufsize", str(video_rate * 2),
            "-vf", f"scale=-2:min(ih\\,{height})", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
            "-threads", "2", str(target), stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
        try:
            await asyncio.wait_for(proc.communicate(), 600)
        except (TimeoutError, asyncio.CancelledError):
            import signal
            os.killpg(proc.pid, signal.SIGTERM)
            await proc.wait()
            raise
        if proc.returncode or not target.is_file() or target.stat().st_size > max_bytes:
            raise ValueError("Video could not be fitted within Telegram's upload limit")
        path.unlink()
        return target

    async def _prepare_media(self, paths: list[Path], max_bytes: int) -> list[Path]:
        """Make visual posts playable in Telegram and standalone audio MP3."""
        prepared: list[Path] = []
        for path in paths:
            check = await verify(path, max_bytes)
            suffix = path.suffix.lower()
            video_codec = ""
            audio_codec = ""
            if check["video"] and suffix == ".mp4":
                probe = await asyncio.create_subprocess_exec(
                    "ffprobe", "-v", "error", "-show_entries",
                    "stream=codec_type,codec_name", "-of", "json",
                    str(path), stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(probe.communicate(), 20)
                if probe.returncode == 0:
                    streams = json.loads(stdout).get("streams", [])
                    video_codec = next(
                        (str(s.get("codec_name") or "") for s in streams
                         if s.get("codec_type") == "video"), ""
                    )
                    audio_codec = next(
                        (str(s.get("codec_name") or "") for s in streams
                         if s.get("codec_type") == "audio"), ""
                    )
            if check["audio"] and not check["video"]:
                if suffix != ".mp3":
                    path = await self._transcode_mp3(path, max_bytes)
            elif check["video"] and (
                suffix != ".mp4" or video_codec not in {"h264", "avc1"}
                or check["audio"] and audio_codec != "aac"
            ):
                target = path.with_name(path.stem + ".telegram.mp4")
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
                    "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "25", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                    "-vf", "scale=min(1280\\,iw):-2", str(target),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
                try:
                    await asyncio.wait_for(proc.communicate(), 300)
                except (TimeoutError, asyncio.CancelledError):
                    import signal
                    os.killpg(proc.pid, signal.SIGTERM)
                    await proc.wait()
                    raise
                if proc.returncode or not target.exists() or target.stat().st_size > max_bytes:
                    raise ValueError("Telegram MP4 conversion failed or exceeds upload limit")
                path.unlink()
                path = target
            elif suffix == ".webp":
                target = path.with_suffix(".jpg")
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
                    "-frames:v", "1", "-q:v", "3", str(target),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.communicate(), 40)
                if proc.returncode or not target.exists() or target.stat().st_size > max_bytes:
                    raise ValueError("Telegram photo conversion failed")
                path.unlink()
                path = target
            if path.suffix.lower() in {".jpg", ".jpeg", ".png"} and path.stat().st_size > 9_000_000:
                target = path.with_name(path.stem + ".telegram.jpg")
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
                    "-frames:v", "1", "-vf", "scale=min(2048\\,iw):-2", "-q:v", "5",
                    str(target), stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.communicate(), 40)
                if proc.returncode or not target.exists() or target.stat().st_size > 9_000_000:
                    raise ValueError("photo exceeds Telegram's native photo limit")
                path.unlink()
                path = target
            prepared.append(path)
        return prepared

    async def _cover(self, url: str, directory: Path) -> Path | None:
        if not url:
            return None
        try:
            target = clean_url(url)
            validate_dns(target)
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                response = await client.get(target)
                if response.status_code != 200 or len(response.content) > 2_000_000:
                    return None
                if not response.headers.get("content-type", "").startswith("image/"):
                    return None
            original = directory / ".cover-source"
            original.write_bytes(response.content)
            output = directory / "cover.jpg"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(original),
                "-vf", "scale=320:320:force_original_aspect_ratio=decrease,pad=320:320:(ow-iw)/2:(oh-ih)/2",
                "-frames:v", "1", "-q:v", "5", str(output),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), 25)
            if proc.returncode == 0 and output.exists() and output.stat().st_size <= 200_000:
                return output
        except (ValueError, httpx.HTTPError, OSError, TimeoutError):
            pass
        return None

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
            "--config-ignore", "--cache-file", ":memory:",
            *instagram_cookie_args(url),
            "-g",
            target,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env=extractor_env(),
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
        media_urls = [clean_url(candidate) for candidate in candidates]
        paths: list[Path] = []
        remaining = gallery_budget(max_bytes)
        for index, media_url in enumerate(media_urls):
            path = await self._direct(media_url, directory, min(max_bytes, remaining), stem=f"{index:02d}")
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
                remaining = gallery_budget(max_bytes)
                for index, candidate in enumerate(candidates):
                    if not isinstance(candidate, str):
                        return None
                    target = clean_url(urljoin("https://www.tikwm.com", candidate))
                    path = await self._direct(target, directory, min(max_bytes, remaining), stem=f"{index:02d}")
                    paths.append(path)
                    remaining -= path.stat().st_size
                if match[1] == "photo":
                    music = data.get("music_info") or {}
                    music_url = music.get("play") if isinstance(music, dict) else None
                    music_url = music_url or data.get("music")
                    if isinstance(music_url, str) and music_url.startswith("https://"):
                        try:
                            sound = await self._direct(
                                clean_url(music_url), directory, min(max_bytes, remaining), stem="sound"
                            )
                            paths.append(sound)
                        except (ValueError, httpx.HTTPError):
                            pass
                author = data.get("author") or {}
                self._save_meta(
                    directory,
                    {
                        "description": data.get("title") or data.get("content_desc") or "",
                        "uploader": author.get("nickname", "") if isinstance(author, dict) else "",
                    },
                )
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

    async def _gallery_public(
        self, url: str, directory: Path, max_bytes: int, *, audio_only: bool = False,
    ) -> list[Path] | None:
        """Fallback for public image/carousel posts absent from yt-dlp."""
        validate_dns(url)
        if not shutil.which("gallery-dl"):
            return None
        proc = await asyncio.create_subprocess_exec(
            "gallery-dl",
            "--config-ignore", "--cache-file", ":memory:",
            *instagram_cookie_args(url),
            *(["-o", "extractor.instagram.audio=merged"] if audio_only else []),
            "-j",
            "--range",
            "1-31",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env=extractor_env(),
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
        try:
            messages = json.loads(stdout)
        except ValueError:
            return None
        urls: list[tuple[str, dict[str, str]]] = []
        for message in messages:
            if not isinstance(message, list) or len(message) < 3 or message[0] != 3:
                continue
            if not isinstance(message[1], str) or not message[1].startswith(("https://", "http://")):
                continue
            meta = message[2] if isinstance(message[2], dict) else {}
            if audio_only and (not meta.get("audio_url") or
                               message[1] != meta.get("audio_url")):
                continue
            urls.append((message[1], gallery_media_headers(meta)))
            if len(urls) == 1 and isinstance(message[2], dict):
                self._save_meta(directory, message[2])
        if not urls or len(urls) > 31:
            return None
        if audio_only:
            urls = urls[:1]
        paths: list[Path] = []
        remaining = gallery_budget(max_bytes)
        for index, (item, headers) in enumerate(urls):
            path = await self._direct(clean_url(item), directory, min(max_bytes, remaining),
                                      stem=f"{index:02d}", headers=headers or None)
            paths.append(path)
            remaining -= path.stat().st_size
        return paths

    async def _ytdlp(
        self, url: str, directory: Path, max_bytes: int, mode: str = "auto"
    ) -> list[Path]:
        validate_dns(url)
        template = str(directory / "%(autonumber)02d.%(ext)s")
        audio_args = (
            ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]
            if mode == "audio"
            else []
        )
        quality_args: list[str] = []
        if source(url) == "youtube" and mode in {"360", "480", "720", "1080"}:
            quality_args = [
                "-f",
                (f"bv[height<={mode}][vcodec^=avc1][ext=mp4]+ba[ext=m4a]/"
                 f"b[height<={mode}][vcodec^=avc1][ext=mp4]/"
                 f"bv[height<={mode}][ext=mp4]+ba[ext=m4a]/"
                 f"b[height<={mode}][ext=mp4]/"
                 f"bv[height<={mode}]+ba/best[height<={mode}]"),
                "--merge-output-format", "mp4",
            ]
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m", "yt_dlp",
            "--ignore-config",
            *instagram_cookie_args(url),
            "--playlist-end",
            "10",
            *(["--concurrent-fragments", "4"] if source(url) == "youtube" else []),
            *(["--no-playlist"] if not collection(url) else []),
            "--no-warnings",
            "--restrict-filenames",
            "--max-filesize",
            str(max_bytes),
            "--write-info-json",
            "-o",
            template,
            *audio_args,
            *quality_args,
            url,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=extractor_env(),
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
        files = [
            p for p in directory.iterdir()
            if p.is_file() and not p.name.endswith((".part", ".info.json"))
            and not p.name.startswith((".mikachan-", ".nex" + "us-"))
        ]
        if not 1 <= len(files) <= 10:
            raise ValueError("source returned no bounded deliverable media")
        return sorted(files)
