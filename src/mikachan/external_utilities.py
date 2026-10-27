"""Verified, user-bound OCR and UtilHubBot relay over the existing user account."""

from __future__ import annotations

import asyncio
import html
import io
import mimetypes
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from aiogram.types import (BufferedInputFile, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message, ReplyParameters)
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


@dataclass
class EditSession:
    user: int
    chat: int
    message: int
    original: int
    upstream: int
    expires: float
    buttons: dict[int, tuple[int, int, str]]
    awaiting_input: bool = False


class ExternalUtilities:
    def __init__(self, bridge: UserbotBridge | None, bot, ensure_staging=None):
        self.bridge, self.bot = bridge, bot
        self.ensure_staging = ensure_staging
        self.sessions: dict[str, StyleSession] = {}
        self.edit_sessions: dict[str, EditSession] = {}

    def _prune(self) -> None:
        now = time.time()
        self.sessions = {key: row for key, row in self.sessions.items() if row.expires > now}
        self.edit_sessions = {key: row for key, row in self.edit_sessions.items()
                              if row.expires > now}
        for rows in (self.sessions, self.edit_sessions):
            while len(rows) > 100:
                rows.pop(next(iter(rows)))

    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            self._prune()

    def _require(self) -> UserbotBridge:
        if self.bridge is None:
            raise ValueError("This command needs the configured Telegram user account session")
        return self.bridge

    async def _stage_reply(self, command: Message, *, image: bool = False,
                           audio: bool = False,
                           any_media: bool = False,
                           source: Message | None = None) -> tuple[int | None, bytes | None, str | None]:
        bridge = self._require()
        source = source or command.reply_to_message
        if source is None:
            raise ValueError("Reply to an image with /ocr" if image else
                             "Reply to an audio or voice message with /asr")
        if image and not (source.photo or source.document and
                          str(source.document.mime_type or "").startswith("image/")):
            raise ValueError("Reply to a photo or image file with /ocr")
        if audio and not (source.audio or source.voice or source.video_note or
                          source.document and str(source.document.mime_type or "").startswith("audio/")):
            raise ValueError("Reply to an audio or voice message with /asr")
        if any_media and not any(getattr(source, name, None) for name in
                                 ("document", "audio", "voice", "video", "video_note",
                                  "animation", "sticker", "photo")):
            raise ValueError("Reply to a media message with /edit")
        media = (source.document or source.audio or source.voice or
                 getattr(source, "video", None) or getattr(source, "animation", None) or
                 getattr(source, "sticker", None) or source.video_note or
                 (source.photo[-1] if source.photo else None))
        if media is None:
            raise ValueError("No supported media was found in the replied message")
        size = int(getattr(media, "file_size", 0) or 0)
        if size > 1_900_000_000:
            raise ValueError("Media exceeds the available relay limit")
        if any_media and bridge.staging_chat_id >= 0 and self.ensure_staging:
            await self.ensure_staging()
        if any_media and bridge.staging_chat_id >= 0:
            raise ValueError("Media editing needs a private transfer chat to preserve its format")
        if size > 20_000_000 and bridge.staging_chat_id >= 0 and self.ensure_staging:
            await self.ensure_staging()
        if bridge.staging_chat_id < 0:
            copy_args = {"caption": ""} if not (getattr(source, "sticker", None) or
                                                  source.video_note or source.voice) else {}
            copied = await self.bot.copy_message(bridge.staging_chat_id, command.chat.id,
                                                 source.message_id, **copy_args)
            return int(copied.message_id), None, None
        if size > 20_000_000:
            raise ValueError("Media above 20 MB needs a private transfer chat")
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
            if bridge.staging_chat_id >= 0:
                size = int(getattr(getattr(item, "file", None), "size", 0) or 0)
                if size > 20_000_000:
                    raise ValueError("Pronunciation audio exceeds the direct upload limit")
                data = await bridge.client.download_media(item, file=bytes)
                if not isinstance(data, bytes) or not 0 < len(data) <= 20_000_000:
                    raise ValueError("Pronunciation audio is unavailable")
                await self.bot.send_audio(command.chat.id,
                    BufferedInputFile(data, "pronunciation.mp3"), caption="",
                    reply_markup=None,
                    reply_parameters=ReplyParameters(message_id=command.message_id))
                continue
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

    @staticmethod
    def _edit_buttons(upstream, token: str) -> tuple[InlineKeyboardMarkup,
                                                     dict[int, tuple[int, int, str]]]:
        rows: list[list[InlineKeyboardButton]] = []
        actions: dict[int, tuple[int, int, str]] = {}
        banned = ("instagram", "اینستاگرام", "ادامه روی همین", "continue on this",
                  "لینک دانلود", "دریافت لینک", "download link", "copy link")
        for row_index, row in enumerate((getattr(upstream, "buttons", None) or [])[:16]):
            rendered = []
            for column, button in enumerate(row[:4]):
                label = str(getattr(button, "text", "") or "").strip()[:64]
                if (not label or any(term in label.casefold() for term in banned)
                        or getattr(button, "data", None) is None):
                    continue
                index = len(actions)
                actions[index] = row_index, column, label
                rendered.append(InlineKeyboardButton(text=label,
                    callback_data=f"ed:{token}:{index}"))
            if rendered:
                rows.append(rendered)
        rows.append([InlineKeyboardButton(text="✖ Close", callback_data=f"ed:{token}:close")])
        return InlineKeyboardMarkup(inline_keyboard=rows), actions

    @staticmethod
    def _clean_edit_text(value: str) -> str:
        lines = [line for line in value.splitlines() if not any(term in line.casefold()
                 for term in ("@abzarmediabot", "downloaded by", "دانلود از اینستاگرام",
                              "دریافت لینک دانلود", "ادامه روی همین"))]
        return "\n".join(lines).strip()[:3500]

    @staticmethod
    def _expects_edit_input(value: str) -> bool:
        text = value.casefold()
        return any(phrase in text for phrase in
                   ("بفرست", "ارسال کن", "وارد کن", "بنویس",
                    "متنی که", "صدایی که", "عکس کاور", "زمان شروع", "زمان پایان",
                    "send the", "enter the", "type the", "reply with", "upload"))

    async def _deliver_edit_media(self, bridge: UserbotBridge, item, chat: int,
                                  original: int) -> None:
        """Small results use bounded Bot API upload; larger ones use private Telegram copy."""
        size = int(getattr(getattr(item, "file", None), "size", 0) or 0)
        media = getattr(item, "media", None)
        document = getattr(media, "document", None)
        mime = str(getattr(document, "mime_type", "") or "")
        method = ("send_photo" if getattr(media, "photo", None) else
                  "send_video" if mime.startswith("video/") else
                  "send_audio" if mime.startswith("audio/") else "send_document")
        if 0 < size <= 45_000_000 and hasattr(bridge, "client"):
            extension = mimetypes.guess_extension(mime) or \
                (".jpg" if method == "send_photo" else ".bin")
            name = os.path.basename(str(getattr(getattr(item, "file", None), "name", "")
                or "result" + extension))[:120]
            with tempfile.TemporaryDirectory(prefix="mikachan-edit-") as directory:
                target = Path(directory) / name
                downloaded = await bridge.client.download_media(item, file=str(target))
                path = Path(downloaded) if isinstance(downloaded, str) else target
                if path.is_file() and path.parent == Path(directory) and \
                        0 < path.stat().st_size <= 45_000_000:
                    parameter = {"send_photo": "photo", "send_video": "video",
                                 "send_audio": "audio", "send_document": "document"}[method]
                    await getattr(self.bot, method)(chat,
                        **{parameter: FSInputFile(path, filename=name)},
                        caption="", reply_markup=None,
                        reply_parameters=ReplyParameters(message_id=original))
                    return
        if bridge.staging_chat_id >= 0 and self.ensure_staging:
            await self.ensure_staging()
        if bridge.staging_chat_id >= 0:
            raise ValueError("Large edited media needs a private transfer chat")
        staged = await bridge.external_stage("edit", item)
        try:
            await self.bot.copy_message(chat, bridge.staging_chat_id, staged,
                caption="", reply_markup=None,
                reply_parameters=ReplyParameters(message_id=original))
        finally:
            try:
                await bridge.instagram_unstage(staged)
            except Exception:
                pass

    async def _edit_result(self, chat: int, user: int, original: int,
                           results: list, *, replace=None) -> None:
        bridge = self._require()
        self._prune()
        media = [item for item in results if getattr(item, "media", None) and
                 getattr(item, "sender_id", None) ==
                 getattr(bridge.external_peers.get("edit"), "id", None)]
        if media:
            for item in media:
                await self._deliver_edit_media(bridge, item, chat, original)
            if replace is not None:
                await self.bot.delete_message(chat, replace)
            return
        upstream = next((item for item in reversed(results)
                         if getattr(item, "buttons", None) or
                         self._clean_edit_text(str(getattr(item, "raw_text", "") or ""))), None)
        if upstream is None:
            raise ValueError("The media editor returned no usable conversion or edit options")
        token = secrets.token_urlsafe(9)
        markup, actions = self._edit_buttons(upstream, token)
        prompt = self._clean_edit_text(str(getattr(upstream, "raw_text", "") or ""))
        if not actions and not prompt:
            raise ValueError("The media editor returned no supported edit options")
        awaiting_input = self._expects_edit_input(prompt)
        if not actions and not awaiting_input:
            await self.bot.send_message(chat, html.escape(prompt),
                reply_parameters=ReplyParameters(message_id=original))
            if replace is not None:
                await self.bot.delete_message(chat, replace)
            return
        body = html.escape(prompt) if prompt else "🛠 <b>Choose an edit</b>"
        if replace is None:
            sent = await self.bot.send_message(chat, body,
                                               reply_markup=markup)
            message_id = sent.message_id
        else:
            await self.bot.edit_message_text(body,
                chat_id=chat, message_id=replace, reply_markup=markup)
            message_id = replace
        self.edit_sessions[token] = EditSession(user, chat, message_id, original,
                                               upstream.id, time.time() + 600, actions,
                                               awaiting_input=awaiting_input or not bool(actions))

    async def _style_result(self, chat: int, user: int, upstream, *, replace=None) -> None:
        token = secrets.token_urlsafe(8)
        markup, actions = self._style_markup(upstream, token)
        text = str(getattr(upstream, "raw_text", "") or "").strip()
        if not text and actions:
            text = "Choose a style"
        if not text:
            raise ValueError("Style provider returned no text")
        if len(text) > 3900:
            raise ValueError("Style provider response exceeds Telegram's menu limit")
        if replace is not None:
            await replace.edit_text(html.escape(text), reply_markup=markup)
            sent = replace
        else:
            sent = await self.bot.send_message(chat, html.escape(text), reply_markup=markup)
        self._prune()
        self.sessions[token] = StyleSession(user, chat, sent.message_id, upstream.id,
                                            time.time() + 600, actions)

    async def command(self, message: Message, cmd: str, arg: str) -> bool:
        if cmd not in {"/ocr", "/tr", "/pron", "/asr", "/style", "/edit"}:
            return False
        self._prune()
        try:
            bridge = self._require()
            staged = None
            media_data = None
            media_name = None
            kind = "ocr" if cmd == "/ocr" else "edit" if cmd == "/edit" else "util"
            command = ""
            reply_text = None
            if cmd == "/ocr":
                if arg:
                    raise ValueError("Use /ocr as a reply to an image")
                staged, media_data, media_name = await self._stage_reply(message, image=True)
            elif cmd == "/edit":
                if arg:
                    raise ValueError("Use /edit as a reply to media")
                staged, media_data, media_name = await self._stage_reply(message,
                                                                          any_media=True)
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
            if cmd == "/edit":
                assert message.from_user is not None
                self.edit_sessions = {key: row for key, row in self.edit_sessions.items()
                                      if (row.chat, row.user) !=
                                      (message.chat.id, message.from_user.id)}
                await self._edit_result(message.chat.id, message.from_user.id,
                                        message.message_id, results)
            elif cmd == "/style":
                assert message.from_user is not None
                upstream = next((item for item in reversed(results)
                                 if getattr(item, "buttons", None)), results[-1])
                await self._style_result(message.chat.id, message.from_user.id, upstream)
            else:
                await self._text_results(message, results, kind=kind,
                                         audio=cmd == "/pron")
        except (ValueError, TelegramAPIError, OSError) as exc:
            detail = str(exc)[:250] if isinstance(exc, ValueError) else \
                "External utility could not complete this request"
            await message.reply("⚠️ " + html.escape(detail))
        return True

    async def reply(self, message: Message) -> bool:
        """Continue the active media-editor conversation with its verified provider."""
        self._prune()
        sender = getattr(message, "from_user", None)
        if sender is None:
            return False
        candidates = [(key, row) for key, row in self.edit_sessions.items()
                      if row.awaiting_input and row.user == sender.id and
                      row.chat == message.chat.id]
        if not candidates:
            return False
        token, session = max(candidates, key=lambda pair: pair[1].expires)
        reply = getattr(message, "reply_to_message", None)
        if message.chat.type != "private" and (reply is None or
                                                reply.message_id != session.message):
            return False
        value = str(message.text or message.caption or "").strip()
        if value.startswith("/"):
            if value.casefold() != "/cancel":
                return False
            self.edit_sessions.pop(token, None)
            await self.bot.delete_message(session.chat, session.message)
            return True
        has_media = any(getattr(message, name, None) for name in
                        ("document", "audio", "voice", "video", "video_note",
                         "animation", "sticker", "photo"))
        if not value and not has_media:
            return False
        staged = None
        try:
            media_data = media_name = None
            if has_media:
                staged, media_data, media_name = await self._stage_reply(message,
                    any_media=True, source=message)
            results = await self._require().external_request("edit", "" if has_media else value,
                staged_id=staged, media_data=media_data, media_name=media_name,
                reply_to_upstream=session.upstream, timeout=180)
            self.edit_sessions.pop(token, None)
            await self._edit_result(session.chat, session.user, session.original,
                                    results, replace=session.message)
        except (ValueError, TelegramAPIError, OSError) as exc:
            detail = str(exc)[:200] if isinstance(exc, ValueError) else \
                "Media editor could not complete this step"
            await message.reply("⚠️ " + html.escape(detail))
        finally:
            if staged is not None:
                try:
                    await self.bot.delete_message(self._require().staging_chat_id, staged)
                except TelegramAPIError:
                    pass
        return True

    async def callback(self, query) -> bool:
        self._prune()
        data = str(query.data or "")
        if data.startswith("ed:"):
            parts = data.split(":")
            session = self.edit_sessions.get(parts[1]) if len(parts) == 3 else None
            if (session is None or session.expires < time.time() or
                    session.user != query.from_user.id or
                    session.chat != query.message.chat.id or
                    session.message != query.message.message_id):
                await query.answer("Edit menu expired or belongs to another user")
                return True
            if parts[2] == "close":
                await query.answer()
                self.edit_sessions.pop(parts[1], None)
                await query.message.delete()
                return True
            if not parts[2].isdigit() or int(parts[2]) not in session.buttons:
                await query.answer("Edit option unavailable")
                return True
            await query.answer()
            edit_row, column, label = session.buttons[int(parts[2])]
            try:
                results = await self._require().external_click(session.upstream,
                    edit_row, column, label, kind="edit")
                self.edit_sessions.pop(parts[1], None)
                await self._edit_result(session.chat, session.user, session.original,
                                        results, replace=session.message)
            except (ValueError, TelegramAPIError, OSError) as exc:
                await self.bot.send_message(session.chat,
                    "⚠️ " + html.escape(str(exc)[:250] if isinstance(exc, ValueError)
                                      else "Media editing is unavailable right now"))
            return True
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
