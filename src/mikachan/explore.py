"""Explicit, bounded TikTok discovery. No media is retained on the server."""

from __future__ import annotations

import asyncio
import html
import random
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


@dataclass
class ExploreSession:
    user_id: int
    chat_id: int
    topic: str
    expires: float
    seen: set[str] = field(default_factory=set)
    message_id: int | None = None
    message_ids: set[int] = field(default_factory=set)
    challenge_id: str = ""
    challenge_cursor: int = 0
    challenge_more: bool = True
    search_cursor: int = 0
    pool: list[dict[str, object]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def topic_from_arg(arg: str) -> str:
    topic = arg.strip().lstrip("#").strip()
    if not topic or len(topic) > 60 or not re.fullmatch(r"[\w\- ]+", topic, re.UNICODE):
        raise ValueError("Use /explore TOPIC, for example /explore Mikasa Ackerman edit.")
    return topic


def _media_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or parts.port or not (
        host.endswith(".tiktokcdn.com") or host.endswith(".tiktokcdn-us.com") or
        host == "tikwm.com" or host.endswith(".tikwm.com")
    ):
        return None
    return value


def _post(item: object) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    ident = str(item.get("video_id") or item.get("id") or "")
    author = item.get("author")
    handle = str(author.get("unique_id") or "") if isinstance(author, dict) else ""
    if not re.fullmatch(r"\d{10,22}", ident) or not re.fullmatch(r"[\w.]{1,64}", handle,
                                                                  re.ASCII):
        return None
    images = [_media_url(url) for url in (item.get("images") or [])[:12]] \
        if isinstance(item.get("images"), list) else []
    images = [url for url in images if url]
    video = _media_url(item.get("play"))
    if not images and not video:
        return None
    duration = item.get("duration")
    if video and isinstance(duration, (int, float)) and duration > 0:
        images = []
    def count(key: str) -> int:
        value = str(item.get(key) or "0").replace(",", "")
        return min(int(value), 1_000_000_000_000) if value.isdigit() else 0

    return {"id": ident, "handle": handle, "images": images, "video": video,
            "title": str(item.get("title") or item.get("content_desc") or "")[:500],
            "score": count("play_count") + 20 * count("digg_count") +
                     30 * count("share_count") + 10 * count("comment_count")}


def _markup(token: str, profile: str | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🔄 One more", callback_data=f"ex:{token}:more")]]
    if profile:
        rows.append([InlineKeyboardButton(text="👤 Creator profile", url=profile)])
    rows.append([InlineKeyboardButton(text="Thanks ✕", callback_data=f"ex:{token}:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class Explore:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.sessions: dict[str, ExploreSession] = {}
        self._challenge_gate = asyncio.Lock()
        self._challenge_at = 0.0

    def _new(self, user_id: int, chat_id: int, topic: str) -> tuple[str, ExploreSession]:
        now = time.monotonic()
        self.sessions = {key: value for key, value in self.sessions.items()
                         if value.expires > now}
        token = secrets.token_urlsafe(9)
        session = ExploreSession(user_id, chat_id, topic, now + 600)
        self.sessions[token] = session
        return token, session

    async def command(self, message: Message, arg: str) -> None:
        if message.from_user is None:
            return
        if not arg.strip():
            token, session = self._new(message.from_user.id, message.chat.id, "")
            sent = await message.reply("🔎 <b>TikTok Explore</b>\n\nAdd a topic with "
                "<code>/explore Mikasa Ackerman edit</code>, or pick a random post?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Yes, random", callback_data=f"ex:{token}:yes"),
                    InlineKeyboardButton(text="No thanks", callback_data=f"ex:{token}:close")]]))
            session.message_id = sent.message_id
            session.message_ids.add(sent.message_id)
            return
        try:
            topic = topic_from_arg(arg)
        except ValueError as exc:
            await message.reply(str(exc))
            return
        token, session = self._new(message.from_user.id, message.chat.id, topic)
        await self.send_next(token, session)

    async def _find(self, session: ExploreSession) -> dict[str, object]:
        available = [post for post in session.pool if post["id"] not in session.seen]
        if available:
            return max(available, key=lambda post: int(str(post.get("score") or 0)))
        if session.challenge_id and not session.challenge_more:
            raise ValueError("No more accessible TikTok posts were found for this topic")
        async with httpx.AsyncClient(timeout=12, follow_redirects=False,
                                     limits=httpx.Limits(max_connections=4)) as client:
            source_success = False
            async def get(path: str, params: dict[str, str | int], *,
                          challenge: bool = False) -> dict:
                nonlocal source_success
                # The public challenge endpoints document a one request per
                # second budget. Serialize across concurrent Explore users.
                if challenge:
                    async with self._challenge_gate:
                        delay = self._challenge_at + 1.2 - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                        self._challenge_at = time.monotonic()
                        response = await client.get("https://www.tikwm.com/api/" + path,
                            params=params, headers={"Accept": "application/json"})
                else:
                    response = await client.get("https://www.tikwm.com/api/" + path,
                    params=params, headers={"Accept": "application/json"})
                response.raise_for_status()
                source_success = True
                if len(response.content) > 2_000_000:
                    raise ValueError("TikTok discovery response is too large")
                payload = response.json()
                return payload if isinstance(payload, dict) and payload.get("code") == 0 else {}

            async def fetch(path: str, params: dict[str, str | int]) -> list:
                payload = await get("feed/" + path, params)
                data = payload.get("data")
                return data if isinstance(data, list) else \
                    data.get("videos", []) if isinstance(data, dict) else []

            items: list = []
            verified_topic = False
            if session.topic and " " not in session.topic:
                try:
                    if not session.challenge_id:
                        searched = await get("challenge/search", {"keywords": session.topic,
                            "count": 10}, challenge=True)
                        choices = (searched.get("data") or {}).get("challenge_list", [])
                        session.challenge_id = next((str(item.get("id")) for item in choices
                            if isinstance(item, dict) and
                            str(item.get("cha_name") or "").casefold() == session.topic.casefold()
                            and str(item.get("id") or "").isdigit()), "")
                    if session.challenge_id:
                        page_payload = await get("challenge/posts", {"challenge_id": session.challenge_id,
                            "count": 30, "cursor": session.challenge_cursor}, challenge=True)
                        data = page_payload.get("data") or {}
                        videos = data.get("videos", [])
                        if isinstance(videos, list):
                            items = videos
                            verified_topic = bool(items)
                            cursor = data.get("cursor")
                            if isinstance(cursor, int) and cursor >= 0:
                                session.challenge_cursor = cursor
                            session.challenge_more = bool(data.get("hasMore"))
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    pass
            if session.topic and not items:
                try:
                    items = await fetch("search", {"keywords": session.topic,
                                                    "count": 30,
                                                    "cursor": session.search_cursor})
                    if items:
                        # A provider search can identify a topic even when the
                        # creator's caption does not repeat every search word.
                        verified_topic = True
                        session.search_cursor += len(items)
                except (httpx.HTTPError, ValueError):
                    pass  # The public search endpoint often returns a 403 challenge.
            if not items:
                batches = await asyncio.gather(*(fetch("list", {"region": "US",
                    "count": 50, "cursor": cursor}) for cursor in (0, 10, 20, 30)),
                    return_exceptions=True)
                for batch in batches:
                    if isinstance(batch, list):
                        items.extend(batch)
                if session.topic:
                    terms = session.topic.casefold().split()
                    items = [item for item in items if isinstance(item, dict) and
                             all(term in str(item.get("title") or item.get(
                                 "content_desc") or "").casefold() for term in terms)]
            if session.topic and items and not verified_topic:
                terms = session.topic.casefold().split()
                items = [item for item in items if isinstance(item, dict) and
                    all(term in str(item.get("title") or item.get(
                        "content_desc") or "").casefold() for term in terms)]
        posts = [post for item in items[:40] if (post := _post(item)) is not None]
        session.pool.extend(post for post in posts if post["id"] not in
                            {existing["id"] for existing in session.pool})
        unseen = [post for post in posts if post["id"] not in session.seen]
        if not unseen:
            if not source_success:
                raise ValueError("TikTok search is blocked from this server right now")
            raise ValueError("No more accessible TikTok posts were found for this topic")
        return max(unseen, key=lambda post: int(str(post.get("score") or 0))) \
            if session.topic else random.choice(unseen)

    async def send_next(self, token: str, session: ExploreSession) -> None:
        if session.lock.locked():
            return
        async with session.lock:
            await self._send_next(token, session)

    async def _send_next(self, token: str, session: ExploreSession) -> None:
        for attempt in range(3):
            try:
                post = await self._find(session)
                ident, handle = str(post["id"]), str(post["handle"])
                topic_label = (("#" if " " not in session.topic else "") +
                               html.escape(session.topic)) if session.topic else "Random"
                caption = ("🎬 <b>TikTok Explore</b>" +
                           " · " + topic_label +
                           "\n👤 @" + html.escape(handle) +
                           ("\n\n" + html.escape(str(post["title"])) if post["title"] else ""))[:900]
                profile = f"https://www.tiktok.com/@{handle}"
                images = post["images"]
                if isinstance(images, list) and images:
                    for image_url in images[:-1]:
                        await self.bot.send_photo(session.chat_id, image_url)
                    sent = await self.bot.send_photo(session.chat_id, images[-1], caption=caption,
                        reply_markup=_markup(token, profile))
                else:
                    sent = await self.bot.send_video(session.chat_id, str(post["video"]),
                        caption=caption, reply_markup=_markup(token, profile))
                session.seen.add(ident)
                session.message_id = sent.message_id
                session.message_ids.add(sent.message_id)
                return
            except TelegramAPIError:
                # Signed CDN URLs may expire. Try the next ranked post first.
                session.seen.add(str(post["id"]))
                if attempt < 2:
                    continue
                await self.bot.send_message(session.chat_id,
                    "⚠️ TikTok returned posts, but Telegram could not deliver their media.")
                return
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                reason = ("TikTok discovery is unavailable right now"
                          if isinstance(exc, httpx.HTTPError) else str(exc))
                await self.bot.send_message(session.chat_id, "⚠️ " + html.escape(reason))
                return

    async def callback(self, query: CallbackQuery) -> bool:
        data = query.data or ""
        if not data.startswith("ex:"):
            return False
        parts = data.split(":")
        session = self.sessions.get(parts[1]) if len(parts) == 3 else None
        if (not session or session.expires < time.monotonic() or
                session.user_id != query.from_user.id or not isinstance(query.message, Message) or
                session.chat_id != query.message.chat.id or
                (query.message.message_id not in session.message_ids and
                 query.message.message_id != session.message_id)):
            await query.answer("Explore menu expired or belongs to someone else")
            return True
        action = parts[2]
        await query.answer()
        if action == "close":
            self.sessions.pop(parts[1], None)
            try:
                await query.message.delete()
            except Exception:
                await query.message.edit_reply_markup(reply_markup=None)
        elif action in {"yes", "more"}:
            await self.send_next(parts[1], session)
        return True
