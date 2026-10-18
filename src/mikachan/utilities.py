"""Explicit Telegram utility actions; fixed providers, bounded input and real format checks."""

import asyncio
import csv
import html
import io
import json
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import jdatetime  # type: ignore[import-untyped]
import qrcode  # type: ignore[import-untyped]
import zxingcpp  # type: ignore[import-not-found]
from bs4 import BeautifulSoup
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (BufferedInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputSticker, Message)
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import Settings
from .market_prices import ASSETS, MarketPrices

MAX_INPUT = 8 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 20_000_000


def weather_label(code: int) -> str:
    groups = {
        "Clear sky": {0}, "Mainly clear": {1}, "Partly cloudy": {2}, "Overcast": {3},
        "Fog": {45, 48}, "Drizzle": {51, 53, 55, 56, 57},
        "Rain": {61, 63, 65, 66, 67}, "Snow": {71, 73, 75, 77},
        "Rain showers": {80, 81, 82}, "Snow showers": {85, 86},
        "Thunderstorm": {95, 96, 99},
    }
    return next((label for label, values in groups.items() if code in values),
                f"WMO weather code {code}")


def _place_key(value: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKD", value.casefold())
                   if not unicodedata.combining(char)).replace("ي", "ی").replace("ك", "ک")


def select_place(places: list[dict[str, Any]], city: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Prefer an exact populated city; show only distinct, relevant alternatives."""
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for place in places:
        if not isinstance(place, dict) or not {"name", "latitude", "longitude"} <= place.keys():
            continue
        key = (_place_key(str(place["name"])), str(place.get("country_code", "")),
               _place_key(str(place.get("admin1", ""))))
        if key not in unique or int(place.get("population") or 0) > int(unique[key].get("population") or 0):
            unique[key] = place
    ranked = sorted(unique.values(), key=lambda p: (
        _place_key(str(p["name"])) != _place_key(city),
        -int(p.get("population") or 0)))
    exact = [p for p in ranked if _place_key(str(p["name"])) == _place_key(city)]
    if len(exact) == 1 or (len(exact) > 1 and
                           int(exact[0].get("population") or 0) >=
                           5 * max(1, int(exact[1].get("population") or 0))):
        return exact[0], ranked[:3]
    if len(ranked) == 1:
        return ranked[0], ranked
    return None, ranked[:3]


def _valid_rate(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Provider returned an invalid rate") from exc
    if not result.is_finite() or not Decimal("1e-18") <= result < Decimal("1e12"):
        raise ValueError("Provider returned an invalid rate")
    return result


def _rate_text(base: str, quote: str, value: Any, provider: str, updated: str) -> str:
    number = _valid_rate(value)
    display = format(number, ",f")
    if "." in display:
        display = display.rstrip("0").rstrip(".")
    return (f"💱 <b>{html.escape(base)} → {html.escape(quote)}</b>\n"
            f"1 {html.escape(base)} = <b>{display}</b> {html.escape(quote)}\n"
            f"Source · {html.escape(provider)}\n"
            f"As of · {html.escape(updated)}")


def _market_card(report: str) -> str:
    """Present existing validated quote calculations without the provider debug dump."""
    lines = report.splitlines()
    if not lines or not lines[0].startswith("Iran market ·"):
        return html.escape(report)
    summary = [line for line in lines[1:] if line and not line.startswith("  ") and
               ": " in line and not line.startswith(("Relayed", "Cash FX"))]
    relayed = sorted({line.strip().split(" via Gheymat:", 1)[0]
                      for line in lines if line.startswith("  ") and " via Gheymat:" in line})
    direct = sorted({line.strip().split(":", 1)[0]
                     for line in lines if line.startswith(("  Wallex direct:", "  Navasan direct:"))})
    fetched = next((line.strip().removeprefix("Relay fetched ").split(";", 1)[0]
                    for line in lines if line.startswith("  Relay fetched ")), "not supplied")
    body = ["💱 <b>Iran market</b> · Toman per unit"]
    body += [html.escape(line) for line in summary]
    if relayed:
        body.append("Relay · Gheymat (" + html.escape(", ".join(relayed[:6])) + ")")
    if direct:
        body.append("Direct · " + html.escape(", ".join(direct)))
    body.append("Snapshot · " + html.escape(fetched))
    body.append("Indicative only · source trade times may be unknown")
    return "\n".join(body)


def qr_png(value: str) -> bytes:
    if not value or len(value.encode()) > 2000:
        raise ValueError("QR text must be 1–2000 UTF-8 bytes")
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    code.add_data(value)
    code.make(fit=True)
    out = io.BytesIO()
    code.make_image(fill_color="black", back_color="white").save(out, format="PNG")
    return out.getvalue()


def open_image(raw: bytes) -> Image.Image:
    if len(raw) > MAX_INPUT:
        raise ValueError("File exceeds the 8 MiB utility limit")
    try:
        image = Image.open(io.BytesIO(raw))
        if image.width * image.height > 20_000_000:
            raise ValueError("Image exceeds the 20 MP utility limit")
        image.verify()
        image = Image.open(io.BytesIO(raw))
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("Only still images are supported here")
        image.load()
        return ImageOps.exif_transpose(image)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Unsupported or damaged image") from exc


def convert_image(raw: bytes, target: str, sticker: bool = False) -> bytes:
    image = open_image(raw)
    if sticker:
        scale = 512 / max(image.size)
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        image = image.resize(size, Image.Resampling.LANCZOS)
        target = "webp"
    if target not in {"png", "jpeg", "webp"}:
        raise ValueError("Supported image formats: png, jpeg, webp")
    if target == "jpeg":
        if image.mode in {"RGBA", "LA", "P"}:
            rgba = image.convert("RGBA")
            base = Image.new("RGB", rgba.size, "white")
            base.paste(rgba, mask=rgba.getchannel("A"))
            image = base
        else:
            image = image.convert("RGB")
    else:
        image = image.convert("RGBA")
    out = io.BytesIO()
    image.save(out, format=target.upper(), quality=90)
    data = out.getvalue()
    if len(data) > MAX_INPUT:
        raise ValueError("Converted file exceeds the 8 MiB utility limit")
    if sticker and len(data) > 512 * 1024:
        raise ValueError("Sticker exceeds Telegram's 512 KiB static-sticker limit")
    check = Image.open(io.BytesIO(data))
    check.verify()
    if (check.format or "").lower() != target:
        raise ValueError("Output format verification failed")
    return data


def decode_codes(raw: bytes) -> list[str]:
    image = open_image(raw).convert("RGB")
    return [code.text for code in zxingcpp.read_barcodes(image)]


def convert_table(raw: bytes, source: str, target: str) -> bytes:
    """The first general conversion matrix: CSV object rows ↔ JSON object arrays."""
    if len(raw) > 1024 * 1024:
        raise ValueError("Table file exceeds 1 MiB")
    if (source, target) == ("csv", "json"):
        text = raw.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows or len(rows) > 5000:
            raise ValueError("CSV needs a header and 1–5000 rows")
        result = json.dumps(rows, ensure_ascii=False, indent=2).encode()
        if not isinstance(json.loads(result), list):
            raise ValueError("JSON validation failed")
        return result
    if (source, target) == ("json", "csv"):
        rows = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(rows, list) or not 0 < len(rows) <= 5000 or not all(
            isinstance(r, dict) and all(isinstance(v, (str, int, float, bool, type(None)))
                                        for v in r.values()) for r in rows):
            raise ValueError("JSON must be an array of 1–5000 flat objects")
        fields = list(dict.fromkeys(k for row in rows for k in row))
        if not fields or not all(isinstance(k, str) for k in fields):
            raise ValueError("JSON needs string field names")
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        result = output.getvalue().encode("utf-8-sig")
        list(csv.DictReader(io.StringIO(result.decode("utf-8-sig"))))
        return result
    raise ValueError("Supported file pairs: CSV → JSON and JSON → CSV")


class Utilities:
    def __init__(self, bot: Bot, settings: Settings, db: sqlite3.Connection):
        self.bot, self.settings, self.db = bot, settings, db
        self.media_slots = asyncio.Semaphore(2)
        self.market_prices = MarketPrices(db, settings.navasan_api_key,
                                         settings.navasan_refresh_seconds)
        self._apod_cache: tuple[dict[str, Any], float] | None = None
        db.execute("CREATE TABLE IF NOT EXISTS utility_packs (name TEXT PRIMARY KEY, owner_id INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS favorite_zones (user_id INTEGER NOT NULL, "
                   "zone TEXT NOT NULL, PRIMARY KEY(user_id,zone))")
        db.execute("CREATE TABLE IF NOT EXISTS provider_choices (market TEXT PRIMARY KEY, "
                   "provider TEXT NOT NULL)")

    def provider(self, market: str) -> str:
        row = self.db.execute("SELECT provider FROM provider_choices WHERE market=?", (market,)).fetchone()
        return str(row[0]) if row else ("frankfurter" if market == "fiat" else "coingecko")

    def set_provider(self, market: str, name: str) -> None:
        choices = {"fiat": {"frankfurter", "open_er_api"},
                   "crypto": {"coingecko", "coinpaprika"}}
        if name not in choices.get(market, set()):
            raise ValueError("Supported providers: fiat frankfurter|open_er_api; crypto coingecko|coinpaprika")
        self.db.execute("INSERT INTO provider_choices VALUES (?,?) ON CONFLICT(market) "
                        "DO UPDATE SET provider=excluded.provider", (market, name))

    async def tgs_to_gif(self, raw: bytes) -> bytes:
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "mikachan.render_tgs",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            output, _ = await asyncio.wait_for(process.communicate(raw), timeout=22)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError("Animated sticker rendering timed out") from None
        if process.returncode or len(output) > MAX_INPUT:
            raise ValueError("This animated TGS sticker could not be rendered safely")
        image = Image.open(io.BytesIO(output))
        if image.format != "GIF" or getattr(image, "n_frames", 1) < 2:
            raise ValueError("Animated GIF validation failed")
        return output

    async def reply_file(self, m: Message) -> tuple[bytes, str]:
        reply = m.reply_to_message
        if not reply:
            raise ValueError("Reply to a photo, sticker, or document")
        item = reply.photo[-1] if reply.photo else reply.sticker or reply.document
        if not item:
            raise ValueError("Reply to a photo, sticker, or document")
        if item.file_size and item.file_size > MAX_INPUT:
            raise ValueError("File exceeds the 8 MiB utility limit")
        out = io.BytesIO()
        await self.bot.download(item, destination=out)
        data = out.getvalue()
        if len(data) > MAX_INPUT:
            raise ValueError("File exceeds the 8 MiB utility limit")
        return data, (reply.document.file_name or "") if reply.document else ""

    async def api(self, url: str, params: dict[str, str | int]) -> dict:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            result = await client.get(url, params=params)
            result.raise_for_status()
            if len(result.content) > 1_000_000:
                raise ValueError("Provider response is too large")
            data = result.json()
            if not isinstance(data, dict):
                raise ValueError("Unexpected provider response")
            return data

    async def reference_rate(self, base: str, quote: str) -> str:
        """Try the chosen free reference source, then the independent alternative."""
        kind = "crypto" if base in {"BTC", "ETH"} and quote in {"USD", "EUR"} else "fiat"
        options = ([self.provider(kind)] + (["coinpaprika", "coingecko"] if kind == "crypto"
                    else ["open_er_api", "frankfurter"]))
        errors = 0
        for provider in dict.fromkeys(options):
            try:
                if provider == "coinpaprika":
                    asset = "btc-bitcoin" if base == "BTC" else "eth-ethereum"
                    data = await self.api(f"https://api.coinpaprika.com/v1/tickers/{asset}",
                                          {"quotes": quote})
                    value, updated = data["quotes"][quote]["price"], data["last_updated"]
                elif provider == "coingecko":
                    asset = "bitcoin" if base == "BTC" else "ethereum"
                    data = await self.api("https://api.coingecko.com/api/v3/simple/price", {
                        "ids": asset, "vs_currencies": quote.lower(),
                        "include_last_updated_at": "true"})
                    info = data[asset]
                    value = info[quote.lower()]
                    updated = datetime.fromtimestamp(info["last_updated_at"], timezone.utc).strftime(
                        "%Y-%m-%d %H:%M UTC")
                elif provider == "open_er_api":
                    data = await self.api(f"https://open.er-api.com/v6/latest/{base}", {})
                    value = data["rates"][quote]
                    updated = datetime.fromtimestamp(data["time_last_update_unix"], timezone.utc).strftime(
                        "%Y-%m-%d %H:%M UTC")
                else:
                    data = await self.api("https://api.frankfurter.dev/v1/latest",
                                          {"base": base, "symbols": quote})
                    value, updated = data["rates"][quote], data["date"]
                return _rate_text(base, quote, value, provider, str(updated))
            except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError, OSError):
                errors += 1
        raise ValueError(f"Rate unavailable from {errors} reference sources. Try again later.")

    async def apod_image(self, url: str) -> bytes:
        if not url.startswith("https://"):
            raise ValueError("NASA returned a non-HTTPS image URL")
        if "nasa-logo" in url.casefold():
            raise ValueError("NASA returned its site logo instead of the APOD image")
        data = bytearray()
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_INPUT:
                        raise ValueError("APOD image exceeds the 8 MiB bot limit")
        await asyncio.to_thread(open_image, bytes(data))
        if Image.open(io.BytesIO(data)).format not in {"JPEG", "PNG", "WEBP"}:
            raise ValueError("APOD image format cannot be sent as a photo")
        return bytes(data)

    async def apod_page(self) -> dict[str, Any]:
        """Read the latest official NASA page; its JSON endpoint can return a logo placeholder."""
        if self._apod_cache and time.monotonic() < self._apod_cache[1]:
            return self._apod_cache[0]
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            response = await client.get("https://science.nasa.gov/apod/")
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise ValueError("NASA APOD page is too large")
        soup = BeautifulSoup(response.text, "html.parser")
        metadata = soup.select_one('meta[property="og:image"]')
        media = str(metadata.get("content", "")) if metadata else ""
        heading = soup.select_one("h2.display-48")
        description = soup.select_one(".media-detail-hero__description")
        date_header = next((cell for cell in soup.select(".media-detail-hero__meta-row th")
                            if cell.get_text(" ", strip=True).casefold().startswith("date")), None)
        credit_header = next((cell for cell in soup.select(".media-detail-hero__meta-row th")
                              if cell.get_text(" ", strip=True).casefold().startswith("credit")), None)
        date_cell = date_header.find_next_sibling("td") if date_header else None
        credit_cell = credit_header.find_next_sibling("td") if credit_header else None
        if not heading or not description or not date_cell:
            raise ValueError("NASA APOD page structure is unavailable")
        date = datetime.strptime(date_cell.get_text(" ", strip=True), "%B %d, %Y").date().isoformat()
        explanation = description.get_text(" ", strip=True).removeprefix("Explanation:").strip()
        explanation = explanation.split("Tomorrow's picture:", 1)[0].strip()
        if not explanation or not media.startswith("https://") or "nasa-logo" in media.casefold():
            raise ValueError("NASA APOD page has no valid media preview")
        video = soup.select_one('.media-detail-hero__media iframe') is not None
        data: dict[str, Any] = {"title": heading.get_text(" ", strip=True), "date": date,
                                "explanation": explanation, "url": media,
                                "media_type": "video" if video else "image",
                                "preview": video, "copyright": credit_cell.get_text(" ", strip=True)
                                if credit_cell else "NASA APOD"}
        self._apod_cache = data, time.monotonic() + 3600
        return data

    async def command(self, m: Message, cmd: str, arg: str) -> bool:
        if cmd not in {"/qr", "/scan", "/ocr", "/image", "/sticker", "/stickerimage",
                       "/gif", "/pack", "/convert", "/weather", "/rate", "/time",
                       "/apod", "/translate"}:
            return False
        try:
            await self._command(m, cmd, arg)
        except (ValueError, KeyError, TypeError, IndexError, httpx.HTTPError, TimeoutError,
                subprocess.SubprocessError, ZoneInfoNotFoundError, UnicodeError, OSError,
                TelegramAPIError) as exc:
            detail = ("External provider request failed. Try again later."
                      if isinstance(exc, httpx.HTTPError) else str(exc)[:300] or type(exc).__name__)
            await m.reply(html.escape(detail))
        return True

    async def _command(self, m: Message, cmd: str, arg: str) -> None:
        data: Any
        if cmd == "/qr":
            value = arg or (m.reply_to_message.text if m.reply_to_message else "") or ""
            if not value:
                raise ValueError("Use /qr TEXT or reply to text with /qr")
            async with self.media_slots:
                image_bytes = await asyncio.to_thread(qr_png, value)
            await m.reply_photo(BufferedInputFile(image_bytes, "qr.png"))
        elif cmd in {"/scan", "/ocr", "/image", "/sticker", "/stickerimage", "/convert"}:
            raw, filename = await self.reply_file(m)
            if cmd == "/scan":
                async with self.media_slots:
                    codes = await asyncio.to_thread(decode_codes, raw)
                await m.reply(html.escape("\n".join(codes)[:3500] if codes else "No readable code found."))
            elif cmd == "/ocr":
                async with self.media_slots:
                    await asyncio.to_thread(open_image, raw)
                    languages = (await asyncio.to_thread(
                        subprocess.run, ["tesseract", "--list-langs"],
                        capture_output=True, text=True, timeout=5)).stdout
                    lang = "eng+fas" if "fas" in languages else "eng"
                    if arg.strip().lower() == "fas" and "fas" not in languages:
                        raise ValueError("Persian OCR data is not installed on this host")
                    data = await asyncio.to_thread(self._ocr, raw, lang)
                await m.reply(html.escape(data[:3500] or "No text recognized.") +
                              ("\nEnglish OCR only on this host." if lang == "eng" else ""))
            elif cmd == "/sticker":
                if not m.reply_to_message or not m.reply_to_message.photo:
                    raise ValueError("Reply to a photo with /sticker")
                async with self.media_slots:
                    data = await asyncio.to_thread(convert_image, raw, "webp", sticker=True)
                await self.bot.send_sticker(m.chat.id, BufferedInputFile(data, "sticker.webp"))
            elif cmd == "/stickerimage":
                sticker = m.reply_to_message.sticker if m.reply_to_message else None
                if not sticker or sticker.is_animated or sticker.is_video:
                    raise ValueError("Only static stickers are supported by /stickerimage")
                async with self.media_slots:
                    image_bytes = await asyncio.to_thread(convert_image, raw, "png")
                await m.reply_document(BufferedInputFile(image_bytes, "sticker.png"))
            elif cmd == "/image":
                target = arg.strip().lower()
                async with self.media_slots:
                    image_bytes = await asyncio.to_thread(convert_image, raw, target)
                await m.reply_document(BufferedInputFile(image_bytes, f"image.{target}"))
            else:
                source = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                target = arg.strip().lower()
                if target not in {"csv", "json"} or source not in {"csv", "json"}:
                    raise ValueError("Reply to a .csv or .json document with /convert json|csv")
                converted = await asyncio.to_thread(convert_table, raw, source, target)
                await m.reply_document(BufferedInputFile(converted,
                                                         f"converted.{target}"))
        elif cmd == "/gif":
            sticker = m.reply_to_message.sticker if m.reply_to_message else None
            if not sticker or not sticker.is_animated:
                raise ValueError("Reply to an animated TGS sticker with /gif")
            raw, _ = await self.reply_file(m)
            async with self.media_slots:
                result = await self.tgs_to_gif(raw)
            await m.reply_document(BufferedInputFile(result, "sticker.gif"))
        elif cmd == "/pack":
            if not m.from_user:
                raise ValueError("User identity unavailable")
            action, _, detail = arg.partition(" ")
            if action == "remove":
                sticker = m.reply_to_message.sticker if m.reply_to_message else None
                if not sticker or not sticker.set_name:
                    raise ValueError("Reply to a sticker from your pack with /pack remove")
                row = self.db.execute("SELECT owner_id FROM utility_packs WHERE name=?",
                                      (sticker.set_name,)).fetchone()
                if not row or row[0] != m.from_user.id:
                    raise ValueError("This pack is not registered to your numeric ID")
                await self.bot.delete_sticker_from_set(sticker.file_id)
                await m.reply("Sticker removed from your pack.")
                return
            parts = [x.strip() for x in detail.split("|")]
            if action not in {"create", "add"} or len(parts) != (3 if action == "create" else 2):
                raise ValueError("Reply to a photo: /pack create SHORTNAME|TITLE|EMOJI or /pack add SHORTNAME|EMOJI")
            shortname = parts[0].lower()
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", shortname):
                raise ValueError("Pack short name must start with a letter and use letters, digits or underscores")
            title = parts[1] if action == "create" else ""
            emoji = parts[-1]
            if not emoji or len(emoji) > 12 or (action == "create" and not 1 <= len(title) <= 60):
                raise ValueError("Provide a title and one short emoji")
            if not m.reply_to_message or not m.reply_to_message.photo:
                raise ValueError("Reply to a photo for a static sticker pack")
            me = await self.bot.get_me()
            if not me.username:
                raise ValueError("Bot username is unavailable")
            name = f"{shortname}_by_{me.username}"
            if len(name) > 64:
                raise ValueError("Pack name is too long for Telegram")
            if action == "add":
                row = self.db.execute("SELECT owner_id FROM utility_packs WHERE name=?", (name,)).fetchone()
                if not row or row[0] != m.from_user.id:
                    raise ValueError("This pack is not registered to your numeric ID")
            raw, _ = await self.reply_file(m)
            async with self.media_slots:
                sticker_bytes = await asyncio.to_thread(convert_image, raw, "webp", sticker=True)
            input_sticker = InputSticker(sticker=BufferedInputFile(sticker_bytes,
                                                               "sticker.webp"), format="static", emoji_list=[emoji])
            if action == "create":
                await self.bot.create_new_sticker_set(m.from_user.id, name, title, [input_sticker])
                self.db.execute("INSERT INTO utility_packs VALUES (?,?)", (name, m.from_user.id))
            else:
                await self.bot.add_sticker_to_set(m.from_user.id, name, input_sticker)
            await m.reply(html.escape(f"Sticker pack updated: https://t.me/addstickers/{name}"))
        elif cmd == "/weather":
            if not 1 <= len(arg) <= 80:
                raise ValueError("Use /weather CITY")
            segments = [part.strip() for part in arg.split(",")]
            if len(segments) > 3:
                raise ValueError("Use /weather CITY[, COUNTRY[, REGION]]")
            city = segments[0]
            country = segments[1] if len(segments) > 1 else ""
            region = segments[2] if len(segments) > 2 else ""
            geo = await self.api("https://geocoding-api.open-meteo.com/v1/search",
                                 {"name": city.strip(), "count": 20,
                                  "language": "fa" if re.search(r"[\u0600-\u06ff]", city) else "en"})
            places = geo.get("results") or []
            if country.strip():
                places = [p for p in places if _place_key(str(p.get("country", ""))) ==
                          _place_key(country.strip()) or str(p.get("country_code", "")).casefold() ==
                          country.strip().casefold()]
            if region:
                places = [p for p in places if _place_key(str(p.get("admin1", ""))) == _place_key(region)]
            if not places:
                raise ValueError("Location not found. Try CITY, COUNTRY (for example Tehran, IR).")
            place, suggestions = select_place(places, city)
            if place is None:
                await m.reply("📍 <b>Choose a location</b>\n" +
                              "\n".join(html.escape(f"• {p['name']}, {p.get('country', '')}, "
                                                   f"{p.get('admin1', '')}") for p in suggestions) +
                              "\nTry /weather CITY, COUNTRY, REGION")
                return
            data = await self.api("https://api.open-meteo.com/v1/forecast", {
                "latitude": str(place["latitude"]), "longitude": str(place["longitude"]),
                "current": "temperature_2m,precipitation,wind_speed_10m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                "forecast_days": 3, "timezone": str(place.get("timezone") or "UTC")})
            now, daily = data["current"], data["daily"]
            def safe(value: Any) -> str:
                return html.escape(str(value))
            lines = [f"🌤 <b>{html.escape(str(place['name']))}, "
                     f"{html.escape(str(place.get('country', '')))}</b>",
                     f"Now · {safe(now['temperature_2m'])} °C · {weather_label(int(now['weather_code']))}",
                     f"Rain {safe(now['precipitation'])} mm  ·  Wind {safe(now['wind_speed_10m'])} km/h",
                     "", "<b>Next 3 days</b>"]
            lines += [f"{safe(d)}: {safe(lo)}–{safe(hi)} °C · rain {safe(rain)} mm" for d, lo, hi, rain in
                      zip(daily["time"], daily["temperature_2m_min"], daily["temperature_2m_max"],
                          daily["precipitation_sum"])]
            lines.append(f"Open-Meteo · {safe(now['time'])} local")
            await m.reply("\n".join(lines))
        elif cmd == "/rate":
            parts = arg.upper().split()
            if len(parts) != 2:
                raise ValueError("Use /rate USD EUR, /rate BTC USD, or /rate USD IRT")
            base, quote = parts
            if quote == "IRT":
                if base not in ASSETS:
                    raise ValueError("Supported Iranian Toman assets: " + ", ".join(ASSETS))
                await m.reply(_market_card(await self.market_prices.report((base,))))
            elif base == "IRT":
                raise ValueError("Use ASSET IRT for an Iranian Toman quote")
            elif "IRR" in {base, quote}:
                raise ValueError("Official IRR reference rates do not represent Iran's open market. "
                                 "Use /rate USD IRT for a Toman market estimate.")
            elif not all(p.isalpha() and len(p) <= 5 for p in parts):
                raise ValueError("Use /rate USD EUR, /rate BTC USD, or /rate USD IRT")
            else:
                await m.reply(await self.reference_rate(base, quote))
        elif cmd == "/time":
            if not m.from_user:
                raise ValueError("User identity unavailable")
            action, _, requested = arg.strip().partition(" ")
            if action in {"add", "remove"}:
                ZoneInfo(requested)
                if action == "add":
                    count = self.db.execute("SELECT COUNT(*) FROM favorite_zones WHERE user_id=?",
                                            (m.from_user.id,)).fetchone()[0]
                    if count >= 10:
                        raise ValueError("Maximum 10 saved time zones")
                    self.db.execute("INSERT OR IGNORE INTO favorite_zones VALUES (?,?)",
                                    (m.from_user.id, requested))
                else:
                    self.db.execute("DELETE FROM favorite_zones WHERE user_id=? AND zone=?",
                                    (m.from_user.id, requested))
                await m.reply("Saved time zones updated.")
                return
            if action == "list":
                zones = [str(r[0]) for r in self.db.execute(
                    "SELECT zone FROM favorite_zones WHERE user_id=? ORDER BY zone", (m.from_user.id,))]
                if not zones:
                    raise ValueError("No saved time zones. Use /time add Asia/Tehran")
                lines = [f"{zone}: {datetime.now(ZoneInfo(zone)):%Y-%m-%d %H:%M %Z}" for zone in zones]
                await m.reply(html.escape("\n".join(lines)))
                return
            zone = arg.strip() or "UTC"
            if len(zone) > 80:
                raise ValueError("Use /time IANA_ZONE, for example /time Asia/Tehran")
            now = datetime.now(ZoneInfo(zone))
            jalali = jdatetime.date.fromgregorian(date=now.date())
            await m.reply(html.escape(f"{zone}: {now:%A %Y-%m-%d %H:%M %Z}\n"
                                      f"Jalali: {jalali:%Y-%m-%d}"))
        elif cmd == "/apod":
            try:
                data = await self.apod_page()
            except (httpx.HTTPError, ValueError, OSError):
                try:
                    data = await self.api("https://api.nasa.gov/planetary/apod",
                                          {"api_key": self.settings.nasa_api_key})
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 500:
                        raise
                    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
                    data = await self.api("https://api.nasa.gov/planetary/apod",
                                          {"api_key": self.settings.nasa_api_key, "date": yesterday})
            title = str(data["title"])
            date = datetime.strptime(str(data["date"]), "%Y-%m-%d").date().isoformat()
            media = str(data.get("url", ""))
            explanation = str(data.get("explanation", "")).strip()
            excerpt = explanation[:420].rsplit(" ", 1)[0] if len(explanation) > 420 else explanation
            if len(explanation) > 420:
                excerpt += "…"
            credit = str(data.get("copyright") or "NASA APOD")
            preview_video = bool(data.get("preview"))
            if "nasa-logo" in media.casefold():
                media = ""
            caption = (f"🌌 <b>NASA · {'APOD video preview' if preview_video else 'Picture of the Day'}</b>\n"
                       f"<b>{html.escape(title[:120])}</b> · {html.escape(date)}\n\n"
                       f"{html.escape(excerpt)}\n\nCredit · {html.escape(credit[:100])}")
            page = "https://science.nasa.gov/apod/" if data.get("preview") is not None else \
                f"https://apod.nasa.gov/apod/ap{date[2:4]}{date[5:7]}{date[8:10]}.html"
            button = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="NASA story ↗", url=page)]])
            if media and (data.get("media_type") == "image" or preview_video):
                try:
                    image = await self.apod_image(media)
                except (ValueError, httpx.HTTPError, OSError):
                    await m.reply(caption + "\n\nImage unavailable here; open the NASA page.",
                                  reply_markup=button)
                else:
                    picture_format = Image.open(io.BytesIO(image)).format
                    extension = "jpg" if picture_format == "JPEG" else "png" if picture_format == "PNG" else "webp"
                    await m.reply_photo(BufferedInputFile(image, f"nasa-apod.{extension}"),
                                        caption=caption[:1000], reply_markup=button)
            else:
                await m.reply(caption + "\n\nOpen the NASA page to view the media.",
                              reply_markup=button)
        elif cmd == "/translate":
            target, _, value = arg.partition(" ")
            target = target.lower()
            if target not in {"en", "fa"}:
                raise ValueError("Use /translate en|fa TEXT (or reply to text)")
            value = value or (m.reply_to_message.text if m.reply_to_message else "") or ""
            if not value or len(value) > 2000:
                raise ValueError("Translation text must be 1–2000 characters")
            if not self.settings.translation_api_url:
                raise ValueError("Translation provider is not configured. Text was not sent.")
            if not self.settings.translation_api_url.startswith("https://"):
                raise ValueError("Translation provider must use HTTPS")
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                translation_reply = await client.post(self.settings.translation_api_url, json={
                    "q": value, "source": "auto", "target": target,
                    "api_key": self.settings.translation_api_key})
                translation_reply.raise_for_status()
                translated = translation_reply.json().get("translatedText")
            if not isinstance(translated, str) or not translated:
                raise ValueError("Translation provider returned no text")
            await m.reply(html.escape(f"{target.upper()} · external translation provider\n{translated[:3500]}"))

    @staticmethod
    def _ocr(raw: bytes, lang: str) -> str:
        result = subprocess.run(["tesseract", "stdin", "stdout", "-l", lang], input=raw,
                                capture_output=True, timeout=20)
        if result.returncode:
            raise ValueError("OCR failed to read this image")
        return result.stdout.decode("utf-8", errors="replace").strip()
