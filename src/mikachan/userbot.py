"""Opt-in MTProto bridge; never log, persist, or forward a user session to a chat."""

import asyncio
import html
import mimetypes
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, Message

from .security import clean_url

LINK = re.compile(r"https://[^\s<>\]\[()]+", re.I)
ASSET_NAMES = ("دینار کویت", "دینار عراق", "ربع سکه", "بیتکوین", "نقره", "سکه",
               "دلار", "تتر", "دوج", "دینار", "یورو", "پوند", "لیر", "طلا", "ین",
               "usdt", "doge", "btc", "xrp", "trx", "usd", "jpy", "eur",
               "gbp", "try", "aed", "eth", "ton", "cad", "aud", "irr")
_ASSET = "(?:" + "|".join(re.escape(item) for item in ASSET_NAMES) + ")"
CURRENCY_QUERY = re.compile(rf"(?:[0-9۰-۹٠-٩][0-9۰-۹٠-٩,٬٫.]*\s+)?{_ASSET}"
                            rf"(?:\s+(?:to|به)\s+{_ASSET})?", re.I)


def is_currency_query(text: str) -> bool:
    return len(text) <= 120 and bool(CURRENCY_QUERY.fullmatch(text.strip()))


def extract_download_url(text: str) -> str:
    """Accept a single public HTTPS link from the expected download response."""
    if not any(word in text.lower() for word in ("download", "stream", "لینک")):
        raise ValueError("File service did not return a download link")
    for match in LINK.finditer(text):
        try:
            url = clean_url(match.group().rstrip(".,;"))
        except ValueError:
            continue
        if url.startswith("https://"):
            return url
    raise ValueError("File service did not return a valid HTTPS download link")


@dataclass(frozen=True)
class CurrencyReply:
    """Validated CoinPJ text and optional image; never exposes the user account/session."""

    text: str  # HTML-escaped, suitable for the bot's HTML parse mode.
    photo: bytes | None = None
    filename: str = "coinpj.jpg"
    photo_failed: bool = False


MAX_CURRENCY_PHOTO_BYTES = 9_000_000  # Below the hosted Bot API's 10 MB photo limit.


def _coinpj_photo_name(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "coinpj.jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "coinpj.png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "coinpj.webp"
    return None


async def coinpj_image(client, answer) -> tuple[bytes | None, str, bool]:
    """Fetch one trusted, bounded CoinPJ image from the verified response message."""
    document = getattr(answer, "document", None)
    has_image = bool(getattr(answer, "photo", None)) or bool(
        document and str(getattr(document, "mime_type", "") or "").lower().startswith("image/"))
    if not has_image:
        return None, "coinpj.jpg", False
    try:
        if document and int(getattr(document, "size", 0) or 0) > MAX_CURRENCY_PHOTO_BYTES:
            raise ValueError("CoinPJ photo is too large")
        data = await client.download_media(answer, file=bytes)
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_CURRENCY_PHOTO_BYTES:
            raise ValueError("CoinPJ photo has invalid size")
        filename = _coinpj_photo_name(data)
        if filename is None:
            raise ValueError("CoinPJ response is not a supported image")
        return data, filename, False
    except Exception:
        # Keep a valid quote available even if this single photo fails.  The
        # caller explicitly discloses this error instead of silently omitting it.
        return None, "coinpj.jpg", True


async def send_currency_reply(message: Message, query: str, result: CurrencyReply) -> None:
    """Send the image *with its caption*, preserving long quotes as follow-up text."""
    if result.photo is None:
        warning = "\n\n⚠️ CoinPJ photo could not be retrieved." if result.photo_failed else ""
        await message.reply(result.text + warning)
        return

    # Telegram photo captions are limited to 1024 displayed characters.  Use a
    # conservative bound on the escaped text, with no truncation of the quote.
    caption = (result.text if len(result.text) <= 1000 else
               "💱 <b>" + html.escape(query) + "</b>")
    try:
        await message.reply_photo(BufferedInputFile(result.photo, result.filename),
                                  caption=caption)
    except TelegramBadRequest:
        # Some image documents are accepted as documents but not as photos.
        try:
            await message.reply_document(BufferedInputFile(result.photo, result.filename),
                                         caption=caption)
        except TelegramBadRequest:
            await message.reply(result.text + "\n\n⚠️ CoinPJ image could not be sent.")
            return
    if caption != result.text:
        await message.reply(result.text)


class UserbotBridge:
    @staticmethod
    def _button_signature(message):
        return tuple(tuple((getattr(button, "text", None),
                            getattr(button, "url", None),
                            getattr(button, "data", None)) for button in row)
                     for row in (getattr(message, "buttons", None) or []))

    @staticmethod
    def _instagram_has_button(message, phrase: str) -> bool:
        return any(phrase in str(getattr(button, "text", "") or "").casefold()
                   for row in (getattr(message, "buttons", None) or []) for button in row)

    def __init__(self, api_id: int, api_hash: str, session: str, staging_chat_id: int):
        from telethon import TelegramClient  # type: ignore[import-untyped]
        from telethon.sessions import StringSession  # type: ignore[import-untyped]

        self.client = TelegramClient(StringSession(session), api_id, api_hash)
        self.staging_chat_id = staging_chat_id
        self.lock = asyncio.Lock()
        self.instagram_lock = asyncio.Lock()
        self.last_request = 0.0
        self.cooldown_until = 0.0
        self.file_peer = None
        self.proxy_peer = None
        self.instagram_peer = None
        self.instagram_last_sent = 0.0
        self.instagram_last_click = 0.0

    async def _ready(self) -> None:
        if time.monotonic() < self.cooldown_until:
            raise ValueError("Userbot service is cooling down; retry shortly")
        if not self.client.is_connected():
            await self.client.connect()
        if not await self.client.is_user_authorized():
            raise ValueError("Userbot session is no longer authorized")

    async def _peer(self):
        if self.file_peer is None:
            peer = await self.client.get_entity("FileToLinkiBot")
            if not getattr(peer, "bot", False):
                raise ValueError("File service username did not resolve to a bot")
            self.file_peer = peer
        return self.file_peer

    async def _proxy_peer(self):
        if self.proxy_peer is None:
            peer = await self.client.get_entity("Dnproxbot")
            if (not getattr(peer, "bot", False)
                    or str(getattr(peer, "username", "")).casefold() != "dnproxbot"):
                raise ValueError("Proxy provider identity could not be verified")
            self.proxy_peer = peer
        return self.proxy_peer

    async def _instagram_peer(self):
        if self.instagram_peer is None:
            peer = await self.client.get_entity("instaRoxinoBot")
            if (not getattr(peer, "bot", False)
                    or str(getattr(peer, "username", "")).casefold() != "instaroxinobot"):
                raise ValueError("Instagram provider identity could not be verified")
            self.instagram_peer = peer
        return self.instagram_peer

    async def _instagram_collect(self, peer, after: int, *, settle: float,
                                 limit: int = 110, watch_edit=None,
                                 stop_on_highlight_done: bool = False,
                                 goal: str = "media") -> list:
        """Collect only this verified bot's subsequent messages, in source order."""
        found: dict[int, object] = {}
        started = time.monotonic()
        deadline = started + (55 if goal == "profile" else 180)
        quiet_until = time.monotonic() + 32
        while time.monotonic() < deadline and len(found) < limit:
            rows = await self.client.get_messages(peer, limit=min(120, limit + 10))
            fresh = []
            for item in rows:
                if getattr(item, "sender_id", None) != peer.id or getattr(item, "id", 0) <= after:
                    continue
                previous = found.get(item.id)
                if previous is None or (item.raw_text != getattr(previous, "raw_text", None) or
                                        self._button_signature(item) !=
                                        self._button_signature(previous)):
                    found[item.id] = item
                    fresh.append(item)
            if watch_edit is not None:
                edited = await self.client.get_messages(peer, ids=watch_edit.id)
                if (edited and getattr(edited, "sender_id", None) == peer.id and
                        (edited.raw_text != watch_edit.raw_text or
                         self._button_signature(edited) != self._button_signature(watch_edit))):
                    previous = found.get(edited.id)
                    if previous is None or (edited.raw_text != getattr(previous, "raw_text", None) or
                                            self._button_signature(edited) !=
                                            self._button_signature(previous)):
                        found[edited.id] = edited
                        fresh.append(edited)
            if fresh:
                quiet_until = time.monotonic() + (max(settle, 8) if goal == "profile" else settle)
                if stop_on_highlight_done and any(
                        "highlight downloaded" in str(getattr(item, "raw_text", "") or "").casefold()
                        for item in fresh):
                    break
                if goal == "profile" and any(
                        self._instagram_has_button(item, "download posts") or
                        self._instagram_has_button(item, "download highlights")
                        for item in found.values()):
                    break
                if goal == "posts" and any(self._instagram_has_button(item, "next 5 posts")
                                           for item in found.values()):
                    break
                if goal == "menu" and any(getattr(item, "buttons", None) for item in fresh):
                    break
            pending = goal == "profile" and any(
                any(word in str(getattr(item, "raw_text", "") or "").casefold()
                    for word in ("please wait", "processing", "در حال", "لطفا صبر"))
                for item in found.values())
            if found and time.monotonic() >= quiet_until and not pending:
                break
            await asyncio.sleep(0.4 if time.monotonic() - started < 6 else 1.0)
        if not found:
            raise ValueError("Instagram provider did not return a supported result")
        return [found[key] for key in sorted(found)]

    async def instagram_request(self, query: str, *, settle: float = 3,
                                goal: str = "media") -> list:
        """One query at a time; the upstream bot requires 29 seconds between links."""
        try:
            async with asyncio.timeout(230):
                async with self.instagram_lock:
                    await self._ready()
                    peer = await self._instagram_peer()
                    delay = self.instagram_last_sent + 30 - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    sent = await self.client.send_message(peer, query)
                    self.instagram_last_sent = time.monotonic()
                    return await self._instagram_collect(peer, sent.id, settle=settle,
                                                         goal=goal)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Instagram provider is unavailable; try again later") from exc

    async def instagram_click(self, message_id: int, row: int, column: int,
                              expected_label: str, *, settle: float = 10,
                              stop_on_highlight_done: bool = False) -> list:
        try:
            async with asyncio.timeout(230):
                async with self.instagram_lock:
                    await self._ready()
                    peer = await self._instagram_peer()
                    original = await self.client.get_messages(peer, ids=message_id)
                    if (not original or getattr(original, "sender_id", None) != peer.id
                            or not original.buttons or not 0 <= row < len(original.buttons)
                            or not 0 <= column < len(original.buttons[row])):
                        raise ValueError("Instagram menu expired; open the profile again")
                    button = original.buttons[row][column]
                    if (str(getattr(button, "text", "")).strip() != expected_label
                            or getattr(button, "data", None) is None):
                        raise ValueError("Instagram menu changed; open the profile again")
                    delay = self.instagram_last_click + 2 - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    recent = await self.client.get_messages(peer, limit=1)
                    after = max(message_id, recent[0].id if recent else message_id)
                    await original.click(row, column)
                    self.instagram_last_click = time.monotonic()
                    try:
                        watch = original if ("highlights" in expected_label.casefold()
                                             or "next 5 posts" in expected_label.casefold()
                                             or expected_label.casefold().startswith(
                                                 ("next", "previous", "prev"))) else None
                        goal = ("posts" if "posts" in expected_label.casefold() else
                                "menu" if "highlights" in expected_label.casefold() or
                                expected_label.casefold().startswith(("next", "previous", "prev"))
                                else "media")
                        return await self._instagram_collect(peer, after, settle=settle,
                            watch_edit=watch, stop_on_highlight_done=stop_on_highlight_done,
                            goal=goal)
                    except ValueError:
                        edited = await self.client.get_messages(peer, ids=message_id)
                        if (edited and getattr(edited, "sender_id", None) == peer.id
                                and (edited.raw_text != original.raw_text
                                     or self._button_signature(edited) != self._button_signature(original))):
                            return [edited]
                        raise
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Instagram provider did not answer; try again later") from exc

    async def instagram_caption(self, message):
        """Press Get the caption and return the edited media message when offered."""
        async with self.instagram_lock:
            await self._ready()
            peer = await self._instagram_peer()
            current = await self.client.get_messages(peer, ids=message.id)
            if not current or getattr(current, "sender_id", None) != peer.id:
                raise ValueError("Instagram media message is no longer available")
            for row_index, row in enumerate(getattr(current, "buttons", None) or []):
                for column, button in enumerate(row):
                    if ("get the caption" not in str(getattr(button, "text", "")).casefold()
                            or getattr(button, "data", None) is None):
                        continue
                    delay = self.instagram_last_click + 2 - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    await current.click(row_index, column)
                    self.instagram_last_click = time.monotonic()
                    for _ in range(30):
                        await asyncio.sleep(0.4)
                        edited = await self.client.get_messages(peer, ids=message.id)
                        if edited and edited.raw_text != current.raw_text:
                            return edited
                    raise ValueError("Instagram caption did not arrive after requesting it")
            return current

    async def instagram_download(self, message, destination: Path,
                                 max_bytes: int) -> Path:
        def bounded(received: int, total: int) -> None:
            if received > max_bytes or total > max_bytes:
                raise ValueError("Instagram media exceeds the temporary download limit")

        async with asyncio.timeout(180):
            async with self.instagram_lock:
                await self._ready()
                peer = await self._instagram_peer()
                current = await self.client.get_messages(peer, ids=message.id)
                if not current or getattr(current, "sender_id", None) != peer.id or not current.media:
                    raise ValueError("Instagram media is no longer available")
                result = await self.client.download_media(current, file=str(destination),
                                                          progress_callback=bounded)
        if not result or Path(result).stat().st_size > max_bytes:
            raise ValueError("Instagram media download failed or exceeded the limit")
        return Path(result)

    async def instagram_stage(self, message_id: int) -> int:
        """Forward to the shared private staging chat for Bot API server-side copy."""
        if self.staging_chat_id >= 0:
            raise ValueError("Instagram fast copy needs a private staging chat")
        async with self.instagram_lock:
            await self._ready()
            peer = await self._instagram_peer()
            current = await self.client.get_messages(peer, ids=message_id)
            if not current or getattr(current, "sender_id", None) != peer.id or not current.media:
                raise ValueError("Instagram media is no longer available")
            staged = await self.client.forward_messages(self.staging_chat_id, current)
            if isinstance(staged, list):
                if len(staged) != 1:
                    raise ValueError("Instagram staging returned an unexpected batch")
                staged = staged[0]
            return int(staged.id)

    async def instagram_unstage(self, message_id: int) -> None:
        if self.staging_chat_id < 0:
            async with self.instagram_lock:
                await self.client.delete_messages(self.staging_chat_id, message_id)

    async def proxy_request(self):
        """Request the provider's own menu through the existing MTProto account."""
        try:
            async with asyncio.timeout(40):
                async with self.lock:
                    await self._ready()
                    peer = await self._proxy_peer()
                    async with self.client.conversation(peer, timeout=30, exclusive=True) as chat:
                        sent = await chat.send_message("/proxy")
                        for _ in range(3):
                            answer = await chat.get_response()
                            if (getattr(answer, "sender_id", None) == peer.id
                                    and getattr(answer, "id", 0) > sent.id):
                                return answer
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Proxy provider is unavailable; try again shortly") from exc
        raise ValueError("Proxy provider returned no verified response")

    async def proxy_click(self, message_id: int, row: int, column: int):
        """Press only a button selected from our bound, verified provider menu."""
        def signature(message):
            return tuple(tuple((button.text, button.url, button.data)
                               for button in line) for line in (message.buttons or []))

        try:
            async with asyncio.timeout(25):
                async with self.lock:
                    await self._ready()
                    peer = await self._proxy_peer()
                    original = await self.client.get_messages(peer, ids=message_id)
                    if (not original or getattr(original, "sender_id", None) != peer.id
                            or not original.buttons or not 0 <= row < len(original.buttons)
                            or not 0 <= column < len(original.buttons[row])
                            or getattr(original.buttons[row][column], "data", None) is None):
                        raise ValueError("Proxy menu is no longer available; send /proxy again")
                    response = await original.click(row, column)
                    original_signature = signature(original)
                    for _ in range(4):
                        await asyncio.sleep(0.5)
                        latest = await self.client.get_messages(peer, limit=1)
                        if (latest and getattr(latest[0], "sender_id", None) == peer.id
                                and latest[0].id > message_id):
                            return latest[0], ""
                        edited = await self.client.get_messages(peer, ids=message_id)
                        if (edited and getattr(edited, "sender_id", None) == peer.id
                                and (edited.raw_text != original.raw_text
                                     or signature(edited) != original_signature)):
                            return edited, ""
                    return original, str(getattr(response, "message", "") or "")[:300]
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Proxy provider did not answer; send /proxy to refresh") from exc

    async def file_to_link(self, bot: Bot, source_chat: int, source_message: int,
                           message: Message | None = None) -> str:
        if self.staging_chat_id >= 0 and message is None:
            raise ValueError("Reply to a media message with /url")
        if source_chat == self.staging_chat_id:
            raise ValueError("Use a different source chat")
        staged = None
        try:
            async with asyncio.timeout(90):
                async with self.lock:
                    await self._ready()
                    peer = await self._peer()
                    delay = self.last_request + 2 - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    if self.staging_chat_id < 0:
                        staged = await bot.copy_message(self.staging_chat_id, source_chat,
                                                        source_message)
                        source = None
                        for _ in range(5):
                            source = await self.client.get_messages(self.staging_chat_id,
                                                                    ids=staged.message_id)
                            if source:
                                break
                            await asyncio.sleep(0.4)
                        if not source or not source.media:
                            raise ValueError("Reply to a file, photo, video, or audio message")
                        return await self._request_file_link(peer, source=source)
                    media = (message.document or message.video or message.audio or
                             message.animation or message.voice or message.video_note or
                             (message.photo[-1] if message.photo else None) or message.sticker)
                    if media is None:
                        raise ValueError("Reply to a file, photo, video, or audio message")
                    size = getattr(media, "file_size", None)
                    if size is not None and size > 20_000_000:
                        raise ValueError("This file needs a private staging chat: set "
                                         "USERBOT_STAGING_CHAT_ID for files over 20 MB")
                    filename = str(getattr(media, "file_name", "") or "")
                    suffix = Path(filename).suffix[:10] or mimetypes.guess_extension(
                        str(getattr(media, "mime_type", "") or "")) or ".jpg"
                    with tempfile.TemporaryDirectory(prefix="mikachan-url-") as temp_dir:
                        path = Path(temp_dir) / ("media" + suffix)
                        await bot.download(media, destination=path, timeout=45)
                        if path.stat().st_size > 20_000_000:
                            raise ValueError("This file needs a private staging chat: set "
                                             "USERBOT_STAGING_CHAT_ID for files over 20 MB")
                        return await self._request_file_link(peer, path=path)
        except asyncio.TimeoutError as exc:
            self.cooldown_until = time.monotonic() + 30
            raise ValueError("File service timed out") from exc
        except ValueError:
            raise
        except Exception as exc:
            self.cooldown_until = time.monotonic() + min(600, max(30, int(getattr(exc, "seconds", 30))))
            raise ValueError("File service is unavailable") from exc
        finally:
            if staged is not None:
                try:
                    await bot.delete_message(self.staging_chat_id, staged.message_id)
                except Exception:
                    pass

    async def _request_file_link(self, peer, *, source=None, path: Path | None = None) -> str:
        async with self.client.conversation(peer, timeout=55, exclusive=True) as chat:
            sent = (await self.client.forward_messages(peer, source) if source is not None
                    else await chat.send_file(str(path)))
            self.last_request = time.monotonic()
            for _ in range(3):
                answer = await chat.get_response()
                if (getattr(answer, "sender_id", None) != peer.id or
                        getattr(answer, "id", 0) <= sent.id):
                    continue
                try:
                    return extract_download_url(str(answer.raw_text or ""))
                except ValueError:
                    continue
        raise ValueError("File service returned no usable link")

    async def upload_and_copy(self, bot: Bot, path: Path, target_chat: int,
                              caption: str | None, buttons: InlineKeyboardMarkup | None,
                              *, video: bool = False):
        """Telegram server-side copy avoids Bot API byte upload limits."""
        if self.staging_chat_id >= 0:
            raise ValueError("Large uploads require a private staging chat")
        if path.stat().st_size > 1_900_000_000:
            raise ValueError("File exceeds the configured MTProto upload limit")
        async with self.lock:
            await self._ready()
            staged = await self.client.send_file(self.staging_chat_id, str(path),
                                                 supports_streaming=video,
                                                 force_document=not video)
            try:
                return await bot.copy_message(target_chat, self.staging_chat_id, staged.id,
                                              caption=caption, reply_markup=buttons)
            finally:
                try:
                    await self.client.delete_messages(self.staging_chat_id, staged.id)
                except Exception:
                    pass

    async def currency_message(self, query: str) -> CurrencyReply:
        """Relay CoinPJ text and its attached photo to the owner-controlled Bot API.

        Retrieve media while holding the userbot's existing single-flight lock.
        Photo failures are reported separately so the original quote survives.
        """
        if len(query) > 120 or "\n" in query:
            raise ValueError("Currency request is too long")
        try:
            async with asyncio.timeout(40):
                async with self.lock:
                    await self._ready()
                    peer = await self.client.get_entity("CoinPJbot")
                    if not getattr(peer, "bot", False):
                        raise ValueError("Currency service is unavailable")
                    delay = self.last_request + 2 - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    async with self.client.conversation(peer, timeout=28, exclusive=True) as chat:
                        sent = await chat.send_message(query)
                        self.last_request = time.monotonic()
                        answer = await chat.get_response()
                    if (getattr(answer, "sender_id", None) != peer.id or
                            getattr(answer, "id", 0) <= sent.id):
                        raise ValueError("Currency response could not be verified")
                    result = str(answer.raw_text or "").strip()
                    if not result or len(result) > 3500:
                        raise ValueError("Currency service returned no readable result")

                    photo, filename, failed = await coinpj_image(self.client, answer)
                    return CurrencyReply(
                        text="💱 <b>" + html.escape(query) + "</b>\n\n" + html.escape(result),
                        photo=photo, filename=filename, photo_failed=failed)
        except asyncio.TimeoutError as exc:
            raise ValueError("Currency service timed out") from exc
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Currency service is unavailable") from exc

    async def currency(self, query: str) -> str:
        """Backwards-compatible text-only API for other existing callers."""
        return (await self.currency_message(query)).text

    async def close(self) -> None:
        if self.client.is_connected():
            await self.client.disconnect()
