"""Explicit, bounded TikTok discovery. No media is retained on the server."""

from __future__ import annotations

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


def topic_from_arg(arg: str) -> str:
    topic = arg.strip().lstrip("#").strip()
    if not topic or len(topic) > 60 or not re.fullmatch(r"[\w\- ]+", topic, re.UNICODE):
        raise ValueError("Use /explore #topic, for example /explore #attackontitan.")
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
    return {"id": ident, "handle": handle, "images": images, "video": video,
            "title": str(item.get("title") or item.get("content_desc") or "")[:500]}


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
                "<code>/explore #attackontitan</code>, or pick a random post?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Yes, random", callback_data=f"ex:{token}:yes"),
                    InlineKeyboardButton(text="No thanks", callback_data=f"ex:{token}:close")]]))
            session.message_id = sent.message_id
            return
        try:
            topic = topic_from_arg(arg)
        except ValueError as exc:
            await message.reply(str(exc))
            return
        token, session = self._new(message.from_user.id, message.chat.id, topic)
        await self.send_next(token, session)

    async def _find(self, session: ExploreSession) -> dict[str, object]:
        url = ("https://www.tikwm.com/api/feed/search" if session.topic else
               "https://www.tikwm.com/api/feed/list")
        params: dict[str, str | int] = ({"keywords": session.topic, "count": 20, "cursor": 0} if session.topic
                  else {"region": "US", "count": 20})
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            response = await client.get(url, params=params,
                                        headers={"Accept": "application/json"})
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise ValueError("TikTok discovery response is too large")
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise ValueError("TikTok discovery is unavailable right now")
        data = payload.get("data")
        items = data if isinstance(data, list) else data.get("videos", []) if isinstance(data, dict) else []
        posts = [post for item in items[:40] if (post := _post(item)) is not None]
        unseen = [post for post in posts if post["id"] not in session.seen]
        if not unseen:
            raise ValueError("No more accessible TikTok posts were found for this topic")
        return random.choice(unseen)

    async def send_next(self, token: str, session: ExploreSession) -> None:
        try:
            post = await self._find(session)
            ident, handle = str(post["id"]), str(post["handle"])
            caption = ("🎬 <b>TikTok Explore</b>" +
                       (" · #" + html.escape(session.topic) if session.topic else " · Random") +
                       "\n👤 @" + html.escape(handle) +
                       ("\n\n" + html.escape(str(post["title"])) if post["title"] else ""))[:900]
            profile = f"https://www.tiktok.com/@{handle}"
            images = post["images"]
            if isinstance(images, list) and images:
                for image in images[:-1]:
                    await self.bot.send_photo(session.chat_id, image)
                sent = await self.bot.send_photo(session.chat_id, images[-1], caption=caption,
                    reply_markup=_markup(token, profile))
            else:
                sent = await self.bot.send_video(session.chat_id, str(post["video"]),
                    caption=caption, reply_markup=_markup(token, profile))
            session.seen.add(ident)
            session.message_id = sent.message_id
        except (httpx.HTTPError, TelegramAPIError, ValueError, TypeError) as exc:
            reason = ("TikTok discovery or media delivery is unavailable right now"
                      if isinstance(exc, (httpx.HTTPError, TelegramAPIError)) else str(exc))
            await self.bot.send_message(session.chat_id, "⚠️ " + html.escape(reason))

    async def callback(self, query: CallbackQuery) -> bool:
        data = query.data or ""
        if not data.startswith("ex:"):
            return False
        parts = data.split(":")
        session = self.sessions.get(parts[1]) if len(parts) == 3 else None
        if (not session or session.expires < time.monotonic() or
                session.user_id != query.from_user.id or not isinstance(query.message, Message) or
                session.chat_id != query.message.chat.id or
                session.message_id != query.message.message_id):
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
