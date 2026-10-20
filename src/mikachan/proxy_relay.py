"""Bounded, user-bound presentation of Dnproxbot's real inline proxy menu."""

import html
import io
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
import qrcode  # type: ignore[import-untyped]
from qrcode.constants import ERROR_CORRECT_L  # type: ignore[import-untyped]

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
    links: tuple[tuple[str, str], ...]


@dataclass
class ProxyTextSession:
    user_id: int
    chat_id: int
    message_id: int
    expires: float
    links: tuple[tuple[str, str], ...]


class ProxyRelay:
    def __init__(self, bridge: UserbotBridge):
        self.bridge = bridge
        self.sessions: dict[str, ProxySession] = {}
        self.text_sessions: dict[str, ProxyTextSession] = {}

    @staticmethod
    def _links(upstream) -> tuple[tuple[str, str], ...]:
        found: list[tuple[str, str]] = []
        for row in (getattr(upstream, "buttons", None) or [])[:15]:
            for button in row[:8]:
                url = proxy_link(str(getattr(button, "url", "") or ""))
                if url and len(url) <= 400 and url not in {item[1] for item in found}:
                    found.append((str(getattr(button, "text", "") or "Proxy")[:64], url))
                if len(found) == 6:
                    return tuple(found)
        return tuple(found)

    def render(self, upstream, *, note: str = "", allow_back: bool = False) -> tuple[str, InlineKeyboardMarkup,
                                                               dict[int, tuple[int, int]]]:
        rows: list[list[InlineKeyboardButton]] = []
        callbacks: dict[int, tuple[int, int]] = {}
        for row_index, upstream_row in enumerate((getattr(upstream, "buttons", None) or [])[:15]):
            row: list[InlineKeyboardButton] = []
            for column, button in enumerate(upstream_row[:8]):
                label = str(getattr(button, "text", "") or "").strip()[:64]
                is_back = "back" in label.casefold() or "بازگشت" in label
                if not label or is_back and not allow_back:
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
        if self._links(upstream):
            rows.append([InlineKeyboardButton(text="📋 All proxies as text",
                                             callback_data="PLACEHOLDER:list")])
        rows.append([InlineKeyboardButton(text="🙏 Thanks", callback_data="PLACEHOLDER:close")])
        return body, InlineKeyboardMarkup(inline_keyboard=rows), callbacks

    def _remember(self, token: str, user: int, chat: int, message: int,
                  upstream, callbacks: dict[int, tuple[int, int]]) -> None:
        now = time.time()
        self.sessions = {key: session for key, session in self.sessions.items()
                         if session.expires > now}
        if len(self.sessions) >= 100:
            self.sessions.pop(next(iter(self.sessions)))
        self.sessions[token] = ProxySession(user, chat, message, upstream.id,
                                            now + 600, callbacks, self._links(upstream))

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
                       sent.message_id, upstream, callbacks)

    @staticmethod
    def _text(links: tuple[tuple[str, str], ...]) -> str:
        lines = ["🛡 <b>Telegram proxies</b>", f"{len(links)} current options · tap a link to connect"]
        for index, (label, url) in enumerate(links, 1):
            lines.extend(["", f"<b>{index}. {html.escape(label)}</b>", html.escape(url)])
        return "\n".join(lines)

    async def _close(self, query) -> None:
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

    async def click(self, query) -> bool:
        data = str(query.data or "")
        if not data.startswith("px:"):
            return False
        parts = data.split(":")
        if len(parts) != 3 or (not parts[2].isdigit() and parts[2] not in
                              {"close", "list", "qr"}):
            await query.answer("Invalid proxy action")
            return True
        token = parts[1]
        if parts[2] == "qr" or token in self.text_sessions:
            item = self.text_sessions.get(token)
            if (item is None or item.expires <= time.time()
                    or item.user_id != query.from_user.id
                    or item.chat_id != query.message.chat.id
                    or item.message_id != query.message.message_id):
                await query.answer("Proxy list expired or belongs to another user")
                return True
            if parts[2] == "close":
                self.text_sessions.pop(token, None)
                await self._close(query)
            elif parts[2] == "qr":
                payload = "\n".join(url for _label, url in item.links)
                if len(payload.encode()) > 2900:
                    await query.answer("Proxy list is too large for one QR code", show_alert=True)
                    return True
                image = qrcode.QRCode(error_correction=ERROR_CORRECT_L, box_size=8, border=2)
                image.add_data(payload)
                image.make(fit=True)
                buffer = io.BytesIO()
                image.make_image(fill_color="black", back_color="white").save(buffer, format="PNG")
                await query.answer()
                await query.message.answer_photo(BufferedInputFile(buffer.getvalue(), "proxies.png"),
                                                  caption="🛡 Proxy list · scan to share")
            else:
                await query.answer("Invalid proxy action")
            return True
        index = int(parts[2]) if parts[2].isdigit() else -1
        session = self.sessions.get(token)
        if (session is None or session.expires <= time.time()
                or session.user_id != query.from_user.id
                or session.chat_id != query.message.chat.id
                or session.message_id != query.message.message_id
                or (parts[2] not in {"close", "list"} and index not in session.callbacks)):
            await query.answer("Proxy menu expired or belongs to another user")
            return True
        if parts[2] == "close":
            self.sessions.pop(token, None)
            await self._close(query)
            return True
        if parts[2] == "list":
            await query.answer()
            if not session.links:
                return True
            replacement = secrets.token_urlsafe(8)
            buttons = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="▣ QR", callback_data=f"px:{replacement}:qr"),
                InlineKeyboardButton(text="🙏 Thanks", callback_data=f"px:{replacement}:close")]])
            sent = await query.message.answer(self._text(session.links), reply_markup=buttons)
            self.text_sessions = {key: value for key, value in self.text_sessions.items()
                                  if value.expires > time.time()}
            if len(self.text_sessions) >= 100:
                self.text_sessions.pop(next(iter(self.text_sessions)))
            self.text_sessions[replacement] = ProxyTextSession(
                session.user_id, session.chat_id, sent.message_id,
                time.time() + 600, session.links)
            return True
        await query.answer()
        row, column = session.callbacks[index]
        upstream, note = await self.bridge.proxy_click(session.upstream_id, row, column)
        body, markup, callbacks = self.render(upstream, note=note, allow_back=True)
        replacement = secrets.token_urlsafe(8)
        await query.message.edit_text(body, reply_markup=self._bind(markup, replacement))
        self.sessions.pop(token, None)
        self._remember(replacement, session.user_id, session.chat_id,
                       session.message_id, upstream, callbacks)
        return True
