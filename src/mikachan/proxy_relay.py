"""Bounded, user-bound presentation of Dnproxbot's real inline proxy menu."""

import html
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .userbot import UserbotBridge


def proxy_link(raw: str) -> str | None:
    """Permit Telegram proxy links only; never relay arbitrary provider URLs."""
    if len(raw) > 1900:
        return None
    try:
        parts = urlsplit(raw)
        if parts.scheme == "tg" and parts.netloc in {"proxy", "socks"}:
            return urlunsplit(("https", "t.me", "/" + parts.netloc, parts.query, ""))
        if (parts.scheme == "https" and parts.hostname in {"t.me", "telegram.me"}
                and parts.path in {"/proxy", "/socks"} and not parts.username
                and not parts.password and parts.port in {None, 443}):
            return raw
    except ValueError:
        return None
    return None


@dataclass
class ProxySession:
    user_id: int
    chat_id: int
    message_id: int
    upstream_id: int
    expires: float
    callbacks: dict[int, tuple[int, int]]


class ProxyRelay:
    def __init__(self, bridge: UserbotBridge):
        self.bridge = bridge
        self.sessions: dict[str, ProxySession] = {}

    def render(self, upstream, *, note: str = "") -> tuple[str, InlineKeyboardMarkup,
                                                               dict[int, tuple[int, int]]]:
        rows: list[list[InlineKeyboardButton]] = []
        callbacks: dict[int, tuple[int, int]] = {}
        for row_index, upstream_row in enumerate((getattr(upstream, "buttons", None) or [])[:15]):
            row: list[InlineKeyboardButton] = []
            for column, button in enumerate(upstream_row[:8]):
                label = str(getattr(button, "text", "") or "").strip()[:64]
                if not label or "back" in label.casefold() or "بازگشت" in label:
                    continue
                url = proxy_link(str(getattr(button, "url", "") or ""))
                if url:
                    row.append(InlineKeyboardButton(text=label, url=url))
                elif getattr(button, "data", None) is not None and len(callbacks) < 80:
                    index = len(callbacks)
                    callbacks[index] = (row_index, column)
                    row.append(InlineKeyboardButton(text=label, callback_data=f"PLACEHOLDER:{index}"))
            if row:
                rows.append(row)
        if not rows:
            raise ValueError("Proxy provider returned no usable proxy menu")
        source_text = str(getattr(upstream, "raw_text", "") or "").strip()[:3000]
        body = "🛡 <b>Mikachan · Proxy</b>\nTap a location to connect."
        if note:
            body += "\n\n" + html.escape(note[:300])
        if source_text:
            body += "\n\n" + html.escape(source_text)
        rows.append([InlineKeyboardButton(text="🙏 Thanks", callback_data="PLACEHOLDER:close")])
        return body, InlineKeyboardMarkup(inline_keyboard=rows), callbacks

    def _remember(self, token: str, user: int, chat: int, message: int,
                  upstream: int, callbacks: dict[int, tuple[int, int]]) -> None:
        now = time.time()
        self.sessions = {key: session for key, session in self.sessions.items()
                         if session.expires > now}
        if len(self.sessions) >= 100:
            self.sessions.pop(next(iter(self.sessions)))
        self.sessions[token] = ProxySession(user, chat, message, upstream,
                                            now + 600, callbacks)

    @staticmethod
    def _bind(markup: InlineKeyboardMarkup, token: str) -> InlineKeyboardMarkup:
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data and button.callback_data.startswith("PLACEHOLDER:"):
                    button.callback_data = f"px:{token}:{button.callback_data.split(':', 1)[1]}"
        return markup

    async def open(self, message) -> None:
        upstream = await self.bridge.proxy_request()
        body, markup, callbacks = self.render(upstream)
        token = secrets.token_urlsafe(8)
        sent = await message.reply(body, reply_markup=self._bind(markup, token))
        self._remember(token, message.from_user.id, message.chat.id,
                       sent.message_id, upstream.id, callbacks)

    async def click(self, query) -> bool:
        data = str(query.data or "")
        if not data.startswith("px:"):
            return False
        parts = data.split(":")
        if len(parts) != 3 or (not parts[2].isdigit() and parts[2] != "close"):
            await query.answer("Invalid proxy action")
            return True
        token = parts[1]
        index = int(parts[2]) if parts[2].isdigit() else -1
        session = self.sessions.get(token)
        if (session is None or session.expires <= time.time()
                or session.user_id != query.from_user.id
                or session.chat_id != query.message.chat.id
                or session.message_id != query.message.message_id
                or (parts[2] != "close" and index not in session.callbacks)):
            await query.answer("Proxy menu expired or belongs to another user")
            return True
        if parts[2] == "close":
            await query.answer()
            self.sessions.pop(token, None)
            try:
                await query.message.delete()
            except Exception:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
            return True
        await query.answer()
        row, column = session.callbacks[index]
        upstream, note = await self.bridge.proxy_click(session.upstream_id, row, column)
        body, markup, callbacks = self.render(upstream, note=note)
        replacement = secrets.token_urlsafe(8)
        await query.message.edit_text(body, reply_markup=self._bind(markup, replacement))
        self.sessions.pop(token, None)
        self._remember(replacement, session.user_id, session.chat_id,
                       session.message_id, upstream.id, callbacks)
        return True
