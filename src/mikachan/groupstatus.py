"""Idempotent observed group-message counts with exact UTC windows."""

import html
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.exceptions import TelegramBadRequest

from .access import AccessRegistry

TEHRAN = ZoneInfo("Asia/Tehran")
CATEGORIES = ("video", "audio", "photo", "animation", "voice", "video_note",
              "sticker", "document", "text", "other")
LABELS = {"video": "🎬 Videos", "audio": "🎵 Audio / Music", "photo": "🖼 Photos",
          "animation": "📼 GIFs / Animations", "voice": "🗣 Voice Messages",
          "video_note": "📹 Video Messages", "sticker": "🎭 Stickers",
          "document": "📁 Documents", "text": "✍️ Text", "other": "▫️ Other"}
ALIASES = {"آمار": "menu", "آمار روزانه": "day", "آمار هفتگی": "week",
           "آمار ماهانه": "month", "آمار سالانه": "annual_choice",
           "آمار 365 روزه": "annual", "آمار سال تقویمی": "years",
           "آمار کل": "all"}


def alias(text: str) -> str | None:
    clean = " ".join(text.strip().replace("ي", "ی").replace("ك", "ک").split())
    clean = clean.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    return ALIASES.get(clean)


def category(m: Message) -> str | None:
    if getattr(m, "new_chat_members", None) or getattr(m, "left_chat_member", None) or \
            getattr(m, "pinned_message", None) or getattr(m, "migrate_to_chat_id", None):
        return None
    for name in CATEGORIES[:-2]:
        if getattr(m, name, None):
            return name
    if getattr(m, "text", None) or getattr(m, "caption", None):
        return "text"
    return "other"


def window(kind: str, instant: datetime, year: int | None = None) -> tuple[float, float]:
    end = instant.astimezone(timezone.utc)
    if kind == "day":
        local = end.astimezone(TEHRAN)
        start = datetime(local.year, local.month, local.day, tzinfo=TEHRAN).astimezone(timezone.utc)
    elif kind in {"week", "month", "annual"}:
        start = end - timedelta(days={"week": 7, "month": 30, "annual": 365}[kind])
    elif kind == "year" and year and 2000 <= year <= end.astimezone(TEHRAN).year:
        start = datetime(year, 1, 1, tzinfo=TEHRAN).astimezone(timezone.utc)
        end = min(end, datetime(year + 1, 1, 1, tzinfo=TEHRAN).astimezone(timezone.utc))
    elif kind == "all":
        start = datetime(2000, 1, 1, tzinfo=timezone.utc)
    else:
        raise ValueError("Invalid statistics window")
    return start.timestamp(), end.timestamp()


class GroupStatus:
    def __init__(self, db: sqlite3.Connection, access: AccessRegistry):
        self.db, self.access = db, access
        db.executescript("""
        CREATE TABLE IF NOT EXISTS observed_group_messages (
          chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, observed REAL NOT NULL,
          category TEXT NOT NULL, forwarded INTEGER NOT NULL,
          tehran_year INTEGER NOT NULL, tehran_month INTEGER NOT NULL, user_id INTEGER,
          PRIMARY KEY(chat_id,message_id));
        CREATE INDEX IF NOT EXISTS observed_group_time ON observed_group_messages(chat_id,observed);
        CREATE TABLE IF NOT EXISTS group_stats_rollup (
          chat_id INTEGER NOT NULL, tehran_year INTEGER NOT NULL,
          tehran_month INTEGER NOT NULL, category TEXT NOT NULL,
          forwarded INTEGER NOT NULL, amount INTEGER NOT NULL,
          PRIMARY KEY(chat_id,tehran_year,tehran_month,category,forwarded));
        CREATE TABLE IF NOT EXISTS group_stats_maintenance (
          chat_id INTEGER PRIMARY KEY, last_run REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS group_observation_start (
          chat_id INTEGER PRIMARY KEY, started REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS known_group_members (
          chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
          display_name TEXT NOT NULL, last_observed REAL NOT NULL,
          PRIMARY KEY(chat_id,user_id));
        CREATE TABLE IF NOT EXISTS group_status_menus (
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
          message_id INTEGER, expires REAL NOT NULL);
        """)
        columns = {str(row[1]) for row in db.execute("PRAGMA table_info(observed_group_messages)")}
        if "user_id" not in columns:
            db.execute("ALTER TABLE observed_group_messages ADD COLUMN user_id INTEGER")
        db.execute("CREATE INDEX IF NOT EXISTS observed_group_user ON "
                   "observed_group_messages(chat_id,user_id,observed)")

    def permitted(self, user: int, chat: int, chat_type: str) -> bool:
        if not self.access.can_access(user, chat, chat_type):
            return False
        if self.access.can_manage(user, chat):
            return True
        try:
            row = self.db.execute("SELECT value FROM group_settings WHERE chat_id=? "
                                  "AND name='groupstatus_user_enabled'", (chat,)).fetchone()
        except sqlite3.OperationalError:
            row = None
        return row is None or row[0] == "1"

    async def show_identity(self, m: Message) -> None:
        """Only report authenticated Telegram fields and messages actually observed here."""
        if m.chat.type not in {"group", "supergroup"} or not m.reply_to_message:
            await m.reply("Reply to a group member's message with /id or آیدی.")
            return
        person = m.reply_to_message.from_user
        if person is None:
            await m.reply("This message has no identifiable Telegram user.")
            return
        count = int(self.db.execute("SELECT COUNT(*) FROM observed_group_messages "
                                    "WHERE chat_id=? AND user_id=?", (m.chat.id, person.id)
                                    ).fetchone()[0])
        known = self.db.execute("SELECT last_observed FROM known_group_members "
                                "WHERE chat_id=? AND user_id=?", (m.chat.id, person.id)).fetchone()
        name = html.escape(str(getattr(person, "full_name", "") or "Unknown")[:80])
        username = str(getattr(person, "username", "") or "")
        role = (self.access.role(person.id) or "Authorized") if self.access.can_access(
            person.id, m.chat.id, m.chat.type) else "Not authorized"
        lines = ["👤 <b>Group member</b>", f"Name · {name}",
                 f"Telegram ID · <code>{person.id}</code>",
                 f"Username · {'@' + html.escape(username) if username else 'Not set'}",
                 f"Mikachan access · {html.escape(role)}",
                 f"Recent observed messages here · {count}"]
        if known:
            lines.append("Last observed here · " + time.strftime(
                "%Y-%m-%d %H:%M UTC", time.gmtime(float(known[0]))))
        lines.append("Counts cover retained observations in this group; older rollups are not per-user.")
        await m.reply("\n".join(lines))

    def observe(self, m: Message) -> bool:
        if m.chat.type not in {"group", "supergroup"} or m.chat.id not in self.access.groups():
            return False
        kind = category(m)
        if kind is None or not m.message_id:
            return False
        if getattr(m.from_user, "id", None) == getattr(getattr(m, "bot", None), "id", None):
            return False
        now = time.time()
        self.db.execute("INSERT OR IGNORE INTO group_observation_start VALUES (?,?)",
                        (m.chat.id, now))
        if m.from_user and m.from_user.id > 0:
            display = str(getattr(m.from_user, "full_name", "") or "")[:80]
            self.db.execute("INSERT INTO known_group_members VALUES (?,?,?,?) "
                "ON CONFLICT(chat_id,user_id) DO UPDATE SET "
                "display_name=excluded.display_name,last_observed=excluded.last_observed",
                (m.chat.id, m.from_user.id, display, now))
        stamped = getattr(m, "date", None)
        when = stamped.timestamp() if isinstance(stamped, datetime) else now
        when = min(now, max(0.0, when))
        local = datetime.fromtimestamp(when, TEHRAN)
        forwarded = bool(getattr(m, "forward_origin", None) or
                         getattr(m, "forward_date", None) or
                         getattr(m, "is_automatic_forward", False))
        inserted = bool(self.db.execute("INSERT OR IGNORE INTO observed_group_messages "
            "(chat_id,message_id,observed,category,forwarded,tehran_year,tehran_month,user_id) "
            "VALUES (?,?,?,?,?,?,?,?)", (m.chat.id, m.message_id, when, kind, int(forwarded),
                                       local.year, local.month,
                                       m.from_user.id if m.from_user else None)).rowcount)
        last = self.db.execute("SELECT last_run FROM group_stats_maintenance WHERE chat_id=?",
                               (m.chat.id,)).fetchone()
        if last is None or now - float(last[0]) >= 3600:
            self.compact(m.chat.id, now)
        return inserted

    def compact(self, chat: int, now: float | None = None) -> int:
        """Keep exact events for 400 days; roll older counts into Tehran calendar months."""
        moment = now if now is not None else time.time()
        cutoff = moment - 400 * 86400
        ids = self.db.execute("SELECT message_id FROM observed_group_messages WHERE chat_id=? "
                              "AND observed<? ORDER BY observed LIMIT 500", (chat, cutoff)).fetchall()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if ids:
                # The indexed time predicate is bounded by the selected oldest ID.
                placeholders = ",".join("?" for _ in ids)
                params = (chat, *(int(r[0]) for r in ids))
                rows = self.db.execute("SELECT tehran_year,tehran_month,category,forwarded,"
                    f"COUNT(*) FROM observed_group_messages WHERE chat_id=? AND message_id IN ({placeholders}) "
                    "GROUP BY tehran_year,tehran_month,category,forwarded", params).fetchall()
                for row in rows:
                    self.db.execute("INSERT INTO group_stats_rollup VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(chat_id,tehran_year,tehran_month,category,forwarded) "
                        "DO UPDATE SET amount=amount+excluded.amount",
                        (chat, *row))
                self.db.execute(f"DELETE FROM observed_group_messages WHERE chat_id=? "
                                f"AND message_id IN ({placeholders})", params)
            self.db.execute("INSERT INTO group_stats_maintenance VALUES (?,?) "
                            "ON CONFLICT(chat_id) DO UPDATE SET last_run=excluded.last_run",
                            (chat, moment))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return len(ids)

    def counts(self, chat: int, kind: str, instant: datetime, year: int | None = None) -> dict[str, int]:
        start, end = window(kind, instant, year)
        rows = self.db.execute("SELECT category,COUNT(*) AS amount,SUM(forwarded) AS forwards "
                               "FROM observed_group_messages WHERE chat_id=? AND observed>=? "
                               "AND observed<? GROUP BY category", (chat, start, end)).fetchall()
        result = {key: 0 for key in CATEGORIES}
        result["forwarded"] = result["total"] = 0
        for row in rows:
            result[str(row["category"])] = int(row["amount"])
            result["total"] += int(row["amount"])
            result["forwarded"] += int(row["forwards"] or 0)
        if kind in {"all", "year"}:
            if kind == "year":
                rolled = self.db.execute("SELECT category,forwarded,amount FROM group_stats_rollup "
                                         "WHERE chat_id=? AND tehran_year=?",
                                         (chat, year)).fetchall()
            else:
                rolled = self.db.execute("SELECT category,forwarded,amount FROM group_stats_rollup "
                                         "WHERE chat_id=?", (chat,)).fetchall()
            for row in rolled:
                amount = int(row["amount"])
                result[str(row["category"])] += amount
                result["total"] += amount
                result["forwarded"] += amount * int(row["forwarded"])
        return result

    def report(self, chat: int, kind: str, instant: datetime,
               year: int | None = None) -> str:
        local = instant.astimezone(TEHRAN)
        start, end = window(kind, instant, year)
        names = {"day": "Daily", "week": "Weekly", "month": "Monthly",
                 "annual": "Rolling Annual", "year": f"Calendar Year {year}", "all": "All-Time"}
        counts = self.counts(chat, kind, instant, year)
        first = self.db.execute("SELECT started FROM group_observation_start WHERE chat_id=?",
                                (chat,)).fetchone()
        if first is None:
            coverage = "Observation has not begun; zero means no recorded data."
        else:
            started = datetime.fromtimestamp(first[0], TEHRAN)
            coverage = f"Observed since {started:%Y/%m/%d %H:%M} Tehran. Earlier history is unavailable."
        lines = [f"📊 <b>{names[kind]} Statistics</b>", "",
                 f"📅 {local:%Y/%m/%d}", f"🕓 {local:%H:%M} · Tehran, Iran", "",
                 f"💬 <b>Total Messages:</b> {counts['total']:,}"]
        lines.extend(f"{LABELS[key]}: {counts[key]:,}" for key in CATEGORIES[:-2])
        lines.append(f"📩 Forwarded Messages: {counts['forwarded']:,}")
        lines.extend(("", f"Window · {datetime.fromtimestamp(start, TEHRAN):%Y/%m/%d %H:%M} "
                      f"to {datetime.fromtimestamp(end, TEHRAN):%Y/%m/%d %H:%M}",
                      html.escape(coverage),
                      "Only updates Telegram delivered to Mikachan are counted. "
                      "Service messages, edits and Mikachan's outgoing messages are excluded. "
                      "Exact events are retained 400 days; older counts use monthly summaries."))
        if kind == "year":
            lines.extend(("", "Monthly totals"))
            for month in range(1, 13):
                month_start = datetime(year or local.year, month, 1, tzinfo=TEHRAN).timestamp()
                next_month = (datetime((year or local.year) + 1, 1, 1, tzinfo=TEHRAN) if month == 12
                              else datetime(year or local.year, month + 1, 1, tzinfo=TEHRAN)).timestamp()
                total = self.db.execute("SELECT COUNT(*) FROM observed_group_messages "
                    "WHERE chat_id=? AND observed>=? AND observed<?",
                    (chat, month_start, min(next_month, end))).fetchone()[0]
                total += self.db.execute("SELECT COALESCE(SUM(amount),0) FROM group_stats_rollup "
                    "WHERE chat_id=? AND tehran_year=? AND tehran_month=?",
                    (chat, year, month)).fetchone()[0]
                lines.append(f"{month:02d} · {total:,}")
        return "\n".join(lines)

    def _buttons(self, token: str, kind: str) -> InlineKeyboardMarkup:
        choices: tuple[tuple[str, str], ...]
        if kind == "menu":
            choices = (("Daily Statistics", "day"), ("Weekly Statistics", "week"),
                       ("Monthly Statistics", "month"), ("Rolling Annual Statistics", "annual"),
                       ("Calendar Year Statistics", "years"), ("All-Time Statistics", "all"))
        elif kind == "years":
            this_year = datetime.now(TEHRAN).year
            choices = tuple((str(y), f"year{y}") for y in range(this_year, this_year - 5, -1)) + \
                      (("◀ Back", "menu"),)
        elif kind == "annual_choice":
            choices = (("Rolling 365 Days", "annual"),
                       ("Gregorian Calendar Year", "years"), ("◀ Back", "menu"))
        else:
            choices = (("🔄 Refresh", kind), ("◀ Back", "menu"), ("✖ Close", "close"))
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=label, callback_data=f"gs:{token}:{code}")]
            for label, code in choices])

    async def show(self, m: Message, kind: str = "menu") -> None:
        if not m.from_user or not self.permitted(m.from_user.id, m.chat.id, m.chat.type):
            return
        if m.chat.type not in {"group", "supergroup"}:
            return
        token = secrets.token_urlsafe(8)
        self.db.execute("INSERT INTO group_status_menus VALUES (?,?,?,?,?)",
                        (token, m.from_user.id, m.chat.id, None, time.time() + 900))
        if kind == "menu":
            body = ("📊 <b>Group Message Statistics</b>\nChoose a period. Counts start "
                    "when Mikachan can observe messages.")
        elif kind in {"years", "annual_choice"}:
            body = ("📅 <b>Annual Statistics</b>\nChoose a reporting period." if
                    kind == "annual_choice" else
                    "📅 <b>Calendar Year</b>\nSelect a Gregorian year.")
        else:
            body = self.report(m.chat.id, kind, datetime.now(timezone.utc))
        sent = await m.reply(body, reply_markup=self._buttons(token, kind))
        self.db.execute("UPDATE group_status_menus SET message_id=? WHERE token=?",
                        (sent.message_id, token))

    async def callback(self, q: CallbackQuery, data: str) -> bool:
        if not data.startswith("gs:"):
            return False
        if not q.from_user or not isinstance(q.message, Message):
            return True
        parts = data.split(":")
        if len(parts) != 3:
            await q.answer()
            return True
        token, kind = parts[1:]
        row = self.db.execute("SELECT 1 FROM group_status_menus WHERE token=? AND user_id=? "
            "AND chat_id=? AND message_id=? AND expires>?", (token, q.from_user.id,
             q.message.chat.id, q.message.message_id, time.time())).fetchone()
        if not row or not self.permitted(q.from_user.id, q.message.chat.id,
                                         q.message.chat.type):
            await q.answer("Menu unavailable")
            return True
        await q.answer()
        if kind == "close":
            self.db.execute("DELETE FROM group_status_menus WHERE token=?", (token,))
            await q.message.edit_reply_markup(reply_markup=None)
            return True
        if kind == "menu":
            body = "📊 <b>Group Message Statistics</b>\nChoose a period."
        elif kind == "years":
            body = "📅 <b>Calendar Year</b>\nSelect a Gregorian year."
        elif kind == "annual_choice":
            body = "📅 <b>Annual Statistics</b>\nChoose a reporting period."
        else:
            year = int(kind[4:]) if kind.startswith("year") and kind[4:].isdigit() else None
            report_kind = "year" if year else kind
            try:
                body = self.report(q.message.chat.id, report_kind,
                                   datetime.now(timezone.utc), year)
            except ValueError:
                return True
        try:
            await q.message.edit_text(body, reply_markup=self._buttons(token, kind))
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        return True
