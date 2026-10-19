"""User-bound local time cards with explicit city selection and calendar settings."""

import html
import json
import secrets
import sqlite3
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import jdatetime  # type: ignore[import-untyped]
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from hijridate import Gregorian, Hijri

from .geocoding import nominatim_places


FIELDS = {
    "gregorian": "Gregorian date",
    "jalali": "Solar Hijri date",
    "imperial": "Imperial Iranian date",
    "hijri": "Lunar Hijri date",
    "japanese": "Japanese era date",
    "new_gregorian": "Gregorian New Year",
    "new_imperial": "Iranian New Year",
    "new_hijri": "Lunar Hijri New Year",
}
DEFAULT_FIELDS = tuple(FIELDS)
CITY_ALIASES = {
    "تهران": ("Tehran", "IR"), "توکیو": ("Tokyo", "JP"),
    "کیوتو": ("Kyoto", "JP"), "اهواز": ("Ahvaz", "IR"),
    "اصفهان": ("Isfahan", "IR"), "بارسلونا": ("Barcelona", "ES"),
}


def _remaining(now: datetime, day: date) -> str:
    seconds = max(0, int((datetime.combine(day, datetime.min.time(), now.tzinfo) - now)
                         .total_seconds()))
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def time_card(city: str, country: str, zone: str, fields: set[str],
              instant: datetime | None = None) -> str:
    now = (instant or datetime.now(ZoneInfo(zone))).astimezone(ZoneInfo(zone))
    solar = jdatetime.date.fromgregorian(date=now.date())
    lunar = Gregorian.fromdate(now.date()).to_hijri()
    lines = [f"🕓 <b>{html.escape(city)}, {html.escape(country)}</b>",
             f"<b>{now:%H:%M:%S}</b> · {html.escape(zone)}"]
    if "gregorian" in fields:
        lines.append(f"📅 Gregorian · {now:%Y/%m/%d}")
    if "jalali" in fields:
        lines.append(f"☀️ Solar Hijri · {solar.year:04d}/{solar.month:02d}/{solar.day:02d}")
    if "imperial" in fields:
        lines.append(f"🏛 Imperial Iranian · {solar.year + 1180:04d}/{solar.month:02d}/{solar.day:02d}")
    if "hijri" in fields:
        lines.append(f"🌙 Lunar Hijri · {lunar.year:04d}/{lunar.month:02d}/{lunar.day:02d}")
    if "japanese" in fields:
        era = f"Reiwa {now.year - 2018}" if now.date() >= date(2019, 5, 1) else "pre-Reiwa"
        lines.append(f"🇯🇵 Japan · {era} · {now:%m/%d}")
    countdowns = []
    if "new_gregorian" in fields:
        countdowns.append("Gregorian · " + _remaining(now, date(now.year + 1, 1, 1)))
    if "new_imperial" in fields:
        next_solar = jdatetime.date(solar.year + 1, 1, 1).togregorian()
        countdowns.append("Iranian · " + _remaining(now, next_solar))
    if "new_hijri" in fields:
        next_lunar = Hijri(lunar.year + 1, 1, 1).to_gregorian().datetuple()
        countdowns.append("Lunar Hijri · " + _remaining(now, date(*next_lunar)))
    if countdowns:
        lines.extend(("", "🎉 <b>Next New Year</b>", *countdowns))
    lines.append("Lunar date: Umm al-Qura calculation; local observance may differ.")
    lines.append("City data · Open-Meteo / OpenStreetMap")
    return "\n".join(lines)


class NowService:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.executescript("""
        CREATE TABLE IF NOT EXISTS now_preferences (
          user_id INTEGER PRIMARY KEY, city TEXT NOT NULL, country TEXT NOT NULL,
          zone TEXT NOT NULL, fields TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS now_prompts (
          user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
          kind TEXT NOT NULL, expires REAL NOT NULL, PRIMARY KEY(user_id,chat_id));
        CREATE TABLE IF NOT EXISTS now_menus (
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
          message_id INTEGER, expires REAL NOT NULL);
        """)

    async def city(self, query: str) -> tuple[str, str, str]:
        if not 1 <= len(query) <= 80:
            raise ValueError("Enter a city name, for example Tehran or تهران.")
        lookup, country = CITY_ALIASES.get(query.casefold(), (query, ""))
        if any(ord(char) > 127 for char in lookup):
            places = await nominatim_places(lookup)
        else:
            async with httpx.AsyncClient(timeout=9, follow_redirects=False) as client:
                response = await client.get("https://geocoding-api.open-meteo.com/v1/search",
                                            params={"name": lookup, "count": 10, "language": "en"})
                response.raise_for_status()
                if len(response.content) > 150_000:
                    raise ValueError("Location response is too large")
                places = response.json().get("results") or []
        if country:
            places = [place for place in places if place.get("country_code") == country]
        exact = [place for place in places if str(place.get("name", "")).casefold() ==
                 lookup.casefold()]
        candidates = exact or places
        if not candidates:
            raise ValueError("City not found. Try CITY, COUNTRY in Latin letters.")
        place = max(candidates, key=lambda item: int(item.get("population") or 0))
        zone = str(place.get("timezone") or "")
        if not zone:
            async with httpx.AsyncClient(timeout=9, follow_redirects=False) as client:
                response = await client.get("https://api.open-meteo.com/v1/forecast", params={
                    "latitude": place["latitude"], "longitude": place["longitude"],
                    "timezone": "auto", "current": "temperature_2m"})
                response.raise_for_status()
                zone = str(response.json().get("timezone") or "")
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("This location has no supported time zone") from exc
        return str(place["name"])[:80], str(place.get("country") or "")[:80], zone

    def preference(self, user: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM now_preferences WHERE user_id=?", (user,)).fetchone()

    def _save_city(self, user: int, place: tuple[str, str, str]) -> None:
        self.db.execute("INSERT INTO now_preferences VALUES (?,?,?,?,?) "
                        "ON CONFLICT(user_id) DO UPDATE SET city=excluded.city, "
                        "country=excluded.country,zone=excluded.zone",
                        (user, *place, json.dumps(DEFAULT_FIELDS)))

    def _fields(self, pref: sqlite3.Row | None) -> set[str]:
        return set(json.loads(pref["fields"])) & FIELDS.keys() if pref else set(DEFAULT_FIELDS)

    async def _prompt(self, m: Message, kind: str) -> None:
        if m.from_user is None:
            return
        sent = await m.reply("📍 Reply with your default city, for example Tehran or تهران.")
        self.db.execute("INSERT INTO now_prompts VALUES (?,?,?,?,?) "
                        "ON CONFLICT(user_id,chat_id) DO UPDATE SET "
                        "message_id=excluded.message_id,kind=excluded.kind,expires=excluded.expires",
                        (m.from_user.id, m.chat.id, sent.message_id, kind, time.time() + 600))

    async def _card(self, m: Message, place: tuple[str, str, str],
                    fields: set[str]) -> None:
        if m.from_user is None:
            return
        token = secrets.token_urlsafe(8)
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚙️ Settings", callback_data=f"n:{token}:settings")]])
        sent = await m.reply(time_card(*place, fields), reply_markup=markup)
        self.db.execute("INSERT INTO now_menus VALUES (?,?,?,?,?)",
                        (token, m.from_user.id, m.chat.id, sent.message_id, time.time() + 86400))

    async def command(self, m: Message, arg: str) -> None:
        if not m.from_user:
            return
        pref = self.preference(m.from_user.id)
        if arg.strip().lower() == "settings":
            if pref:
                await self._card(m, (pref["city"], pref["country"], pref["zone"]),
                                 self._fields(pref))
            else:
                await self._prompt(m, "default")
            return
        if arg:
            place = await self.city(arg)
            if pref is None:
                self._save_city(m.from_user.id, place)
                pref = self.preference(m.from_user.id)
            await self._card(m, place, self._fields(pref))
        elif pref:
            await self._card(m, (pref["city"], pref["country"], pref["zone"]),
                             self._fields(pref))
        else:
            await self._prompt(m, "default")

    async def reply(self, m: Message) -> bool:
        if not m.from_user or not m.reply_to_message or not m.text:
            return False
        prompt = self.db.execute("SELECT * FROM now_prompts WHERE user_id=? AND chat_id=? "
                                 "AND message_id=? AND expires>?",
                                 (m.from_user.id, m.chat.id, m.reply_to_message.message_id,
                                  time.time())).fetchone()
        if not prompt:
            return False
        try:
            place = await self.city(m.text.strip())
        except (ValueError, httpx.HTTPError) as exc:
            await m.reply(html.escape(str(exc) if isinstance(exc, ValueError) else
                                      "Location service is unavailable. Try again later."))
            return True
        self.db.execute("DELETE FROM now_prompts WHERE user_id=? AND chat_id=?",
                        (m.from_user.id, m.chat.id))
        self._save_city(m.from_user.id, place)
        pref = self.preference(m.from_user.id)
        await self._card(m, place, self._fields(pref))
        return True

    def _settings(self, token: str, fields: set[str]) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(text=f"{'✅' if key in fields else '⬜'} {label}",
                    callback_data=f"n:{token}:toggle:{key}")] for key, label in FIELDS.items()]
        rows.append([InlineKeyboardButton(text="📍 Change default city",
                                         callback_data=f"n:{token}:city")])
        rows.append([InlineKeyboardButton(text="◀ Back", callback_data=f"n:{token}:back"),
                     InlineKeyboardButton(text="✕ Close", callback_data=f"n:{token}:close")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def callback(self, q: CallbackQuery, data: str) -> bool:
        if not data.startswith("n:"):
            return False
        if not q.from_user or not isinstance(q.message, Message):
            return True
        parts = data.split(":")
        if len(parts) < 3:
            await q.answer()
            return True
        row = self.db.execute("SELECT * FROM now_menus WHERE token=? AND user_id=? "
                              "AND chat_id=? AND message_id=? AND expires>?",
                              (parts[1], q.from_user.id, q.message.chat.id,
                               q.message.message_id, time.time())).fetchone()
        if not row:
            await q.answer("This menu belongs to another user or has expired.")
            return True
        await q.answer()
        pref = self.preference(q.from_user.id)
        if pref is None:
            return True
        fields = self._fields(pref)
        action = parts[2]
        if action == "toggle" and len(parts) == 4 and parts[3] in FIELDS:
            field = parts[3]
            fields.remove(field) if field in fields else fields.add(field)
            self.db.execute("UPDATE now_preferences SET fields=? WHERE user_id=?",
                            (json.dumps(sorted(fields)), q.from_user.id))
            action = "settings"
        if action == "settings":
            await q.message.edit_text("⚙️ <b>Time card settings</b>\nChoose the fields to show.",
                                      reply_markup=self._settings(parts[1], fields))
        elif action == "back":
            await q.message.edit_text(time_card(pref["city"], pref["country"], pref["zone"], fields),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="⚙️ Settings",
                                         callback_data=f"n:{parts[1]}:settings")]]))
        elif action == "city":
            sent = await q.message.answer("📍 Reply with your new default city.")
            self.db.execute("INSERT INTO now_prompts VALUES (?,?,?,?,?) "
                            "ON CONFLICT(user_id,chat_id) DO UPDATE SET "
                            "message_id=excluded.message_id,kind=excluded.kind,expires=excluded.expires",
                            (q.from_user.id, q.message.chat.id, sent.message_id, "change",
                             time.time() + 600))
        elif action == "close":
            self.db.execute("DELETE FROM now_menus WHERE token=?", (parts[1],))
            await q.message.edit_reply_markup(reply_markup=None)
        return True
