"""Opt-in MTProto bridge; never log, persist, or forward a user session to a chat."""

import asyncio
import html
import re
import time
from pathlib import Path

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

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


class UserbotBridge:
    def __init__(self, api_id: int, api_hash: str, session: str, staging_chat_id: int):
        from telethon import TelegramClient  # type: ignore[import-untyped]
        from telethon.sessions import StringSession  # type: ignore[import-untyped]

        self.client = TelegramClient(StringSession(session), api_id, api_hash)
        self.staging_chat_id = staging_chat_id
        self.lock = asyncio.Lock()
        self.last_request = 0.0
        self.cooldown_until = 0.0
        self.file_peer = None

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

    async def file_to_link(self, bot: Bot, source_chat: int, source_message: int) -> str:
        if self.staging_chat_id >= 0:
            raise ValueError("File links require a private staging chat")
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
                    async with self.client.conversation(peer, timeout=55, exclusive=True) as chat:
                        sent = await self.client.forward_messages(peer, source)
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

    async def currency(self, query: str) -> str:
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
                    return "💱 <b>" + html.escape(query) + "</b>\n\n" + html.escape(result)
        except asyncio.TimeoutError as exc:
            raise ValueError("Currency service timed out") from exc
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Currency service is unavailable") from exc

    async def close(self) -> None:
        if self.client.is_connected():
            await self.client.disconnect()
