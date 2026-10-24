"""Verified, user-bound OCR and UtilHubBot relay over the existing user account."""

from __future__ import annotations

import html
import io
import mimetypes
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.exceptions import TelegramAPIError

from .userbot import UserbotBridge

_LANG = re.compile(r"[a-z]{2,3}(?:-[a-z]{2})?\Z")
_WORD = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*\Z")


@dataclass
class StyleSession:
    user: int
    chat: int
    message: int
    upstream: int
    expires: float
    buttons: dict[int, tuple[int, int, str]]


class ExternalUtilities:
    def __init__(self, bridge: UserbotBridge | None, bot):
        self.bridge, self.bot = bridge, bot
        self.sessions: dict[str, StyleSession] = {}

    def _require(self) -> UserbotBridge:
        if self.bridge is None:
            raise ValueError("This command needs the configured Telegram user account session")
        return self.bridge

    async def _stage_reply(self, command: Message, *, image: bool = False,
                           audio: bool = False) -> tuple[int | None, bytes | None, str | None]:
        bridge = self._require()
        source = command.reply_to_message
        if source is None:
            raise ValueError("Reply to an image with /ocr" if image else
                             "Reply to an audio or voice message with /asr")
        if image and not (source.photo or source.document and
                          str(source.document.mime_type or "").startswith("image/")):
            raise ValueError("Reply to a photo or image file with /ocr")
        if audio and not (source.audio or source.voice or source.video_note or
                          source.document and str(source.document.mime_type or "").startswith("audio/")):
            raise ValueError("Reply to an audio or voice message with /asr")
        media = (source.document or source.audio or source.voice or source.video_note or
                 (source.photo[-1] if source.photo else None))
        size = int(getattr(media, "file_size", 0) or 0)
        if size > 50_000_000:
            raise ValueError("Media exceeds the 50 MB provider limit")
        if bridge.staging_chat_id < 0:
            copied = await self.bot.copy_message(bridge.staging_chat_id, command.chat.id,
                                                 source.message_id, caption="")
            return int(copied.message_id), None, None
        if size > 20_000_000:
            raise ValueError("Media above 20 MB needs USERBOT_STAGING_CHAT_ID")
        buffer = io.BytesIO()
        await self.bot.download(media, destination=buffer, timeout=45)
        data = buffer.getvalue()
        if not data or len(data) > 20_000_000:
            raise ValueError("Media download is empty or exceeds 20 MB")
        if source.voice:
            extension = ".ogg"
        else:
            extension = mimetypes.guess_extension(str(getattr(media, "mime_type", "") or "")) or \
                (".jpg" if image else ".mp3")
        return None, data, "source" + extension

    async def _text_results(self, command: Message, results: list, *, kind: str,
                            audio: bool = False) -> None:
        lines = [str(getattr(item, "raw_text", "") or "").strip() for item in results]
        lines = [line for line in lines if line]
        text = "\n\n".join(lines)
        if text:
            for offset in range(0, len(text), 3500):
                await command.reply(html.escape(text[offset:offset + 3500]))
        for item in results if audio else []:
            if not getattr(item, "media", None):
                continue
            bridge = self._require()
            staged = await bridge.external_stage(kind, item)
            try:
                await self.bot.copy_message(command.chat.id, bridge.staging_chat_id,
                                            staged, caption="", reply_markup=None)
            finally:
                try:
                    await bridge.instagram_unstage(staged)
                except Exception:
                    pass
        if not text and not (audio and any(getattr(item, "media", None) for item in results)):
            raise ValueError("The provider returned no readable result")

    def _style_markup(self, upstream, token: str) -> tuple[InlineKeyboardMarkup,
                                                              dict[int, tuple[int, int, str]]]:
        rows = []
        actions: dict[int, tuple[int, int, str]] = {}
        for row_index, row in enumerate((getattr(upstream, "buttons", None) or [])[:20]):
            rendered = []
            for column, button in enumerate(row[:8]):
                label = str(getattr(button, "text", "") or "").strip()[:64]
                if not label:
                    continue
                url = str(getattr(button, "url", "") or "")
                try:
                    parsed = urlsplit(url)
                except ValueError:
                    continue
                if url and parsed.scheme == "https" and parsed.hostname:
                    rendered.append(InlineKeyboardButton(text=label, url=url))
                elif getattr(button, "data", None) is not None and len(actions) < 80:
                    index = len(actions)
                    actions[index] = row_index, column, label
                    rendered.append(InlineKeyboardButton(text=label,
                                                         callback_data=f"uh:{token}:{index}"))
            if rendered:
                rows.append(rendered)
        rows.append([InlineKeyboardButton(text="🙏 Thanks", callback_data=f"uh:{token}:close")])
        return InlineKeyboardMarkup(inline_keyboard=rows), actions

    async def _style_result(self, chat: int, user: int, upstream, *, replace=None) -> None:
        token = secrets.token_urlsafe(8)
        markup, actions = self._style_markup(upstream, token)
        text = str(getattr(upstream, "raw_text", "") or "").strip()
        if not text:
            raise ValueError("Style provider returned no text")
        if len(text) > 3900:
            raise ValueError("Style provider response exceeds Telegram's menu limit")
        if replace is not None:
            await replace.edit_text(html.escape(text), reply_markup=markup)
            sent = replace
        else:
            sent = await self.bot.send_message(chat, html.escape(text), reply_markup=markup)
        self.sessions = {key: value for key, value in self.sessions.items()
                         if value.expires > time.time()}
        if len(self.sessions) >= 100:
            self.sessions.pop(next(iter(self.sessions)))
        self.sessions[token] = StyleSession(user, chat, sent.message_id, upstream.id,
                                            time.time() + 600, actions)

    async def command(self, message: Message, cmd: str, arg: str) -> bool:
        if cmd not in {"/ocr", "/tr", "/pron", "/asr", "/style"}:
            return False
        try:
            bridge = self._require()
            staged = None
            media_data = None
            media_name = None
            kind = "ocr" if cmd == "/ocr" else "util"
            command = ""
            reply_text = None
            if cmd == "/ocr":
                if arg:
                    raise ValueError("Use /ocr as a reply to an image")
                staged, media_data, media_name = await self._stage_reply(message, image=True)
            elif cmd == "/asr":
                if arg:
                    raise ValueError("Use /asr as a reply to audio")
                staged, media_data, media_name = await self._stage_reply(message, audio=True)
                command = "/asr"
            elif cmd == "/tr":
                lang, _, value = arg.strip().partition(" ")
                lang = lang.casefold()
                if not _LANG.fullmatch(lang):
                    raise ValueError("Use /tr LANGUAGE TEXT or reply with /tr LANGUAGE")
                value = value.strip()
                if value:
                    command = f"/tr {lang} {value}"
                else:
                    source = message.reply_to_message
                    reply_text = str((source.text or source.caption) if source else "").strip()
                    if not reply_text:
                        raise ValueError("Reply to text with /tr LANGUAGE")
                    command = f"/tr {lang}"
            elif cmd == "/pron":
                if not _WORD.fullmatch(arg.strip()):
                    raise ValueError("Use /pron ENGLISH_WORD")
                command = f"/pron {arg.strip()}"
            elif cmd == "/style":
                if not 1 <= len(arg.strip()) <= 2500:
                    raise ValueError(f"Use {cmd} TEXT (1–2500 characters)")
                command = f"{cmd} {arg.strip()}"
            try:
                results = await bridge.external_request(kind, command, staged_id=staged,
                    reply_text=reply_text, timeout=130 if cmd == "/asr" else 65,
                    media_data=media_data, media_name=media_name)
            finally:
                if staged is not None:
                    try:
                        await self.bot.delete_message(bridge.staging_chat_id, staged)
                    except TelegramAPIError:
                        pass
            if cmd == "/style":
                assert message.from_user is not None
                await self._style_result(message.chat.id, message.from_user.id, results[-1])
            else:
                await self._text_results(message, results, kind=kind,
                                         audio=cmd == "/pron")
        except (ValueError, TelegramAPIError, OSError) as exc:
            detail = str(exc)[:250] if isinstance(exc, ValueError) else \
                "External utility could not complete this request"
            await message.reply("⚠️ " + html.escape(detail))
        return True

    async def callback(self, query) -> bool:
        data = str(query.data or "")
        if not data.startswith("uh:"):
            return False
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Invalid style action")
            return True
        token, action = parts[1:]
        row = self.sessions.get(token)
        if (row is None or row.expires < time.time() or row.user != query.from_user.id or
                row.chat != query.message.chat.id or row.message != query.message.message_id):
            await query.answer("Style menu expired or belongs to another user")
            return True
        if action == "close":
            self.sessions.pop(token, None)
            await query.answer()
            try:
                await query.message.delete()
            except TelegramAPIError:
                await query.message.edit_reply_markup(reply_markup=None)
            return True
        if not action.isdigit() or int(action) not in row.buttons:
            await query.answer("Style option unavailable")
            return True
        await query.answer("Loading…")
        try:
            results = await self._require().external_click(row.upstream, *row.buttons[int(action)])
            self.sessions.pop(token, None)
            await self._style_result(row.chat, row.user, results[-1], replace=query.message)
        except (ValueError, TelegramAPIError, OSError) as exc:
            detail = str(exc)[:200] if isinstance(exc, ValueError) else \
                "Style action could not be completed"
            await self.bot.send_message(row.chat, "⚠️ " + html.escape(detail))
        return True
