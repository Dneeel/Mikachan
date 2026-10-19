"""Optional owner-authorized Telegram user session for CoinPJ Toman quotes."""

import asyncio
import html
import re
import time
from decimal import Decimal, InvalidOperation

from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon.sessions import StringSession  # type: ignore[import-untyped]

from .market_prices import ASSETS

PROMPTS = {
    "USD": "دلار", "EUR": "یورو", "AED": "درهم", "SEKE": "سکه", "GOLD18": "طلا",
    "USDT": "USDT", "BTC": "BTC", "JPY": "ین", "TRY": "لیر", "OMR": "ریال عمان",
    "IQD": "دینار عراق", "GBP": "پوند", "SAR": "ریال سعودی", "INR": "روپیه",
    "ETH": "ETH", "TON": "TON",
}
RESPONSE_NAMES = {
    "USD": ("دلار",), "EUR": ("یورو",), "AED": ("درهم",), "SEKE": ("سکه",),
    "GOLD18": ("طلا",), "USDT": ("USDT", "تتر"), "BTC": ("BTC", "بیت"),
    "JPY": ("ین",), "TRY": ("لیر",), "OMR": ("عمان",), "IQD": ("عراق",),
    "GBP": ("پوند",), "SAR": ("سعودی",), "INR": ("روپیه",),
    "ETH": ("ETH", "اتریوم"), "TON": ("TON", "GRAM"),
}
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _decimal(raw: str) -> Decimal:
    try:
        number = Decimal(raw.translate(_DIGITS).replace(",", "").replace("٬", "")
                         .replace("٫", "."))
    except InvalidOperation as exc:
        raise ValueError("CoinPJ returned an invalid price") from exc
    if not number.is_finite() or not 0 < number < Decimal("1000000000000000"):
        raise ValueError("CoinPJ returned an invalid price")
    return number


def coinpj_prompt(symbol: str, amount: Decimal) -> str:
    if symbol not in PROMPTS or not amount.is_finite() or not 0 < amount <= 1_000_000_000:
        raise ValueError("Unsupported CoinPJ quote")
    quantity = format(amount, "f").rstrip("0").rstrip(".") if amount % 1 else str(int(amount))
    return f"{quantity} {PROMPTS[symbol]}"


def parse_coinpj(text: str, symbol: str, amount: Decimal) -> str:
    if symbol not in ASSETS or len(text) > 4000:
        raise ValueError("CoinPJ quote is unavailable")
    normalized = text.translate(_DIGITS)
    heading = re.search(r"✨\s*([\d,.٬٫]+)\s+([^:\n]{1,100})\s*:", normalized)
    toman = re.search(r"💸\s*([\d,.٬٫]+)\s*(?:toman|تومان)", normalized, re.I)
    if not heading or not toman or _decimal(heading[1]) != amount:
        raise ValueError("CoinPJ returned a different or incomplete quote")
    label = heading[2].upper()
    if not any(name.upper() in label for name in RESPONSE_NAMES[symbol]):
        raise ValueError("CoinPJ asset identity does not match the request")
    price = _decimal(toman[1])
    dollar = re.search(r"💵\s*\$\s*([\d,.٬٫]+)\s*dollar", normalized, re.I)
    stamp = re.search(r"🪙\s*([\d/ -]{8,25})\s*(?:\||·)\s*([\d:]{4,12})", normalized)
    as_of = f"{stamp[1].strip()} · {stamp[2].strip()}" if stamp else "time not supplied"
    amount_text = format(amount, ",f").rstrip("0").rstrip(".") if amount % 1 else f"{int(amount):,}"
    price_text = format(price, ",f").rstrip("0").rstrip(".") if price % 1 else f"{int(price):,}"
    dollar_line = ""
    if dollar:
        value = _decimal(dollar[1])
        dollar_text = format(value, ",f").rstrip("0").rstrip(".") if value % 1 else f"{int(value):,}"
        dollar_line = f"💵 ≈ {html.escape(dollar_text)} USD\n"
    return (f"💱 <b>{html.escape(amount_text)} {html.escape(ASSETS[symbol])}</b>\n"
            f"💸 <b>{html.escape(price_text)} Toman</b>\n{dollar_line}\n"
            f"Source · CoinPJ\nReported · {html.escape(as_of)}\n"
            "Informational quote · provider time and execution price are unverified")


class CoinPJ:
    def __init__(self, api_id: int, api_hash: str, session: str):
        self.client = TelegramClient(StringSession(session), api_id, api_hash)
        self.lock = asyncio.Lock()
        self.peer = None
        self.cache: dict[tuple[str, Decimal], tuple[str, float]] = {}
        self.unavailable_until = 0.0
        self.last_sent = 0.0

    async def quote(self, symbol: str, amount: Decimal) -> str:
        prompt = coinpj_prompt(symbol, amount)
        try:
            async with asyncio.timeout(38):
                async with self.lock:
                    key = (symbol, amount)
                    cached = self.cache.get(key)
                    if cached and time.monotonic() < cached[1]:
                        return cached[0]
                    if time.monotonic() < self.unavailable_until:
                        raise ValueError("CoinPJ is cooling down after a failed request")
                    if not self.client.is_connected():
                        await self.client.connect()
                    if not await self.client.is_user_authorized():
                        raise ValueError("CoinPJ user session is no longer authorized")
                    if self.peer is None:
                        peer = await self.client.get_entity("CoinPJbot")
                        if not getattr(peer, "bot", False):
                            raise ValueError("CoinPJ username does not resolve to a bot")
                        self.peer = peer
                    wait = self.last_sent + 2 - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    async with self.client.conversation(self.peer, timeout=20, exclusive=True) as chat:
                        sent = await chat.send_message(prompt)
                        self.last_sent = time.monotonic()
                        answer = await chat.get_response()
                    if (getattr(answer, "sender_id", None) != getattr(self.peer, "id", None) or
                            getattr(answer, "reply_to_msg_id", None) not in {None, sent.id} or
                            getattr(answer, "id", 0) <= sent.id):
                        raise ValueError("CoinPJ response could not be bound to this request")
                    card = parse_coinpj(str(answer.raw_text or ""), symbol, amount)
                    if len(self.cache) >= 128:
                        self.cache.clear()
                    self.cache[key] = card, time.monotonic() + 30
                    return card
        except asyncio.TimeoutError as exc:
            self.unavailable_until = time.monotonic() + 120
            raise ValueError("CoinPJ timed out; public market estimate may be used") from exc
        except ValueError:
            raise
        except Exception as exc:
            flood = getattr(exc, "seconds", None)
            self.unavailable_until = time.monotonic() + (min(600, max(30, int(flood)))
                                                        if isinstance(flood, int) else 30)
            raise ValueError("CoinPJ is unavailable. Try again later.") from exc

    async def close(self) -> None:
        if self.client.is_connected():
            await self.client.disconnect()
