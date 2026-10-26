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
        self.identity_edits: dict[int, tuple[int, float]] = {}
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
        CREATE TABLE IF NOT EXISTS group_identity_menus (
          token TEXT PRIMARY KEY, actor_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
          message_id INTEGER, target_id INTEGER NOT NULL, real_name TEXT NOT NULL,
          username TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS intel_profiles (
          user_id INTEGER PRIMARY KEY, official_first TEXT NOT NULL DEFAULT '',
          official_last TEXT NOT NULL DEFAULT '', friendly TEXT NOT NULL DEFAULT '',
          birthday TEXT NOT NULL DEFAULT '', birthplace TEXT NOT NULL DEFAULT '',
          residence TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS group_nicknames (
          chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, nickname TEXT NOT NULL,
          PRIMARY KEY(chat_id,user_id));
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

    async def reply(self, m: Message) -> bool:
        if not m.from_user or m.from_user.id not in self.identity_edits or \
                m.chat.type != "private" or self.access.role(m.from_user.id) != "OWNER":
            return False
        target, expires = self.identity_edits[m.from_user.id]
        if expires < time.time():
            self.identity_edits.pop(m.from_user.id, None)
            return False
        if not m.text or m.text.startswith("/"):
            return False
        field, sep, value = m.text.partition("=")
        allowed = {"official_first", "official_last", "friendly", "birthday",
                   "birthplace", "residence"}
        if not sep or field.strip() not in allowed or len(value.strip()) > 100:
            await m.reply("Use FIELD=VALUE. Allowed fields: " + ", ".join(sorted(allowed)))
            return True
        field = field.strip()
        self.db.execute("INSERT OR IGNORE INTO intel_profiles(user_id) VALUES (?)", (target,))
        self.db.execute(f"UPDATE intel_profiles SET {field}=? WHERE user_id=?",
                        (value.strip(), target))
        self.identity_edits.pop(m.from_user.id, None)
        await m.reply("✅ Identity field saved. Reopen /id in the group to view it.")
        return True

    async def show_identity(self, m: Message) -> None:
        """Only report authenticated Telegram fields and messages actually observed here."""
        if m.chat.type not in {"group", "supergroup"} or not m.reply_to_message or \
                not m.from_user:
            await m.reply("Reply to a group member's message with /id or آیدی.")
            return
        person = m.reply_to_message.from_user
        if person is None:
            await m.reply("This message has no identifiable Telegram user.")
            return
        token = secrets.token_urlsafe(8)
        name = str(getattr(person, "full_name", "") or "Unknown")[:80]
        username = str(getattr(person, "username", "") or "")[:80]
        self.db.execute("INSERT INTO group_identity_menus VALUES (?,?,?,?,?,?,?,?)",
                        (token, m.from_user.id, m.chat.id, None, person.id, name,
                         username, time.time() + 900))
        body, buttons = self.identity_screen(token, m.chat.id, person.id, name,
                                             username, "summary", m.from_user.id)
        sent = await m.reply(body, reply_markup=buttons)
        self.db.execute("UPDATE group_identity_menus SET message_id=? WHERE token=?",
                        (sent.message_id, token))

    def identity_screen(self, token: str, chat: int, user: int, name: str,
                        username: str, page: str, actor: int) -> tuple[str, InlineKeyboardMarkup]:
        def controls(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
            return InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=label, callback_data=f"gi:{token}:{action}")]
                for label, action in items])
        nick = self.db.execute("SELECT nickname FROM group_nicknames WHERE chat_id=? AND user_id=?",
                               (chat, user)).fetchone()
        profile = self.db.execute("SELECT * FROM intel_profiles WHERE user_id=?", (user,)).fetchone()
        role = (self.access.role(user) or "Authorized") if self.access.can_access(
            user, chat, "supergroup") else "Not authorized"
        now = datetime.now(timezone.utc)
        day, _ = window("day", now)
        count = self.db.execute("SELECT COUNT(*) FROM observed_group_messages WHERE "
                                "chat_id=? AND user_id=? AND observed>=?",
                                (chat, user, day)).fetchone()[0]
        if page == "summary":
            body = ("🔮 <b>Summary</b>\n\n"
                f"👤 Real name | {html.escape(name)}\n"
                f"👤 Nickname | {html.escape(nick[0]) if nick else 'Not set'}\n"
                f"🔗 Username | {'@' + html.escape(username) if username else 'Not set'}\n"
                f"🆔 Numeric | <code>{user}</code>\n"
                f"🛡️ Access | {html.escape(role)}\n"
                f"💬 Messages today | {count}")
            buttons = [("🗃 Details", "details")]
            if self.access.can_manage(actor, chat):
                buttons.append(("🛡 Manage", "manage"))
            buttons.append(("✖ Close", "close"))
            return body, controls(buttons)
        if page == "details":
            def field(key: str) -> str:
                return html.escape(str(profile[key]) if profile and profile[key] else "Not set")
            counts = []
            for label, kind in (("Today", "day"), ("Last 7 days", "week"),
                                ("Last 30 days", "month"), ("Last 365 days", "annual"),
                                ("All retained", "all")):
                start, end = window(kind, now)
                value = self.db.execute("SELECT COUNT(*) FROM observed_group_messages "
                    "WHERE chat_id=? AND user_id=? AND observed>=? AND observed<?",
                    (chat, user, start, end)).fetchone()[0]
                counts.append(f"💬 {label} | {value}")
            first_last = self.db.execute("SELECT MIN(observed),MAX(observed) FROM "
                "observed_group_messages WHERE chat_id=? AND user_id=?", (chat, user)).fetchone()
            def date(value: float | None) -> str:
                return datetime.fromtimestamp(value, TEHRAN).strftime("%Y/%m/%d") if value else "Unknown"
            try:
                visible = self.db.execute("SELECT c.name,COUNT(*) FROM intel_hits h "
                    "JOIN intel_terms t ON t.id=h.term_id "
                    "JOIN intel_categories c ON c.id=t.category_id "
                    "WHERE h.chat_id=? AND h.user_id=? AND c.visible=1 GROUP BY c.id",
                    (chat, user)).fetchall()
            except sqlite3.OperationalError:
                visible = []
            body = ("🗃️ <b>IDENTITY</b>\n"
                f"🪪 Official name | {field('official_first')}\n"
                f"🪪 Official last name | {field('official_last')}\n"
                f"🫂 Friendly name | {field('friendly')}\n"
                f"👤 Real name | {html.escape(name)}\n"
                f"🎂 Birthday | {field('birthday')}\n"
                f"👶🏻 Place of birth | {field('birthplace')}\n"
                f"📍 Lives in | {field('residence')}\n\n"
                "📊 <b>Observed statistics</b>\n"
                f"💬 First message | {date(first_last[0])}\n"
                f"💬 Last message | {date(first_last[1])}\n" + "\n".join(counts) +
                "\n" + "\n".join(f"🫯 {html.escape(r[0])} | {r[1]}" for r in visible) +
                "\nCounts cover messages this bot observed; earlier history is unavailable.")
            buttons = [("◀ Back", "summary")]
            if self.access.role(actor) == "OWNER":
                buttons.insert(0, ("✏️ Edit identity", "edit"))
                buttons.insert(1, ("🗃 Owner word records", "intel"))
            return body, controls(buttons)
        body = (f"🛡 <b>Manage</b>\n{html.escape(name)} · <code>{user}</code>\n"
                f"Access · {html.escape(role)}\n\n"
                "Use Owner Settings → Access Control for global access. "
                "Use /nickname in this group for its local display name.")
        options: list[tuple[str, str]] = []
        if self.access.role(actor) == "OWNER" and user != self.access.primary_owner and \
                self.access.person(user):
            body += "\nPrivate chat · " + ("On" if self.access.private_enabled(user) else "Off")
            options.append(("🔓 Enable private chat" if not self.access.private_enabled(user)
                            else "🔒 Disable private chat", "private"))
        return body, controls(options + [("◀ Back", "summary")])

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
                       ("Calendar Year Statistics", "years"), ("All-Time Statistics", "all"),
                       ("✖ Close", "close"))
        elif kind == "years":
            this_year = datetime.now(TEHRAN).year
            choices = tuple((str(y), f"year{y}") for y in range(this_year, this_year - 5, -1)) + \
                      (("◀ Back", "menu"), ("✖ Close", "close"))
        elif kind == "annual_choice":
            choices = (("Rolling 365 Days", "annual"),
                       ("Gregorian Calendar Year", "years"), ("◀ Back", "menu"),
                       ("✖ Close", "close"))
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
        if data.startswith("gi:"):
            if not q.from_user or not isinstance(q.message, Message):
                return True
            parts = data.split(":", 2)
            if len(parts) != 3:
                await q.answer()
                return True
            token, action = parts[1:]
            row = self.db.execute("SELECT * FROM group_identity_menus WHERE token=? "
                "AND actor_id=? AND chat_id=? AND message_id=? AND expires>?",
                (token, q.from_user.id, q.message.chat.id, q.message.message_id,
                 time.time())).fetchone()
            if not row or not self.access.can_access(q.from_user.id, q.message.chat.id,
                                                     q.message.chat.type):
                await q.answer("Unavailable")
                return True
            if action == "close":
                await q.message.delete()
                await q.answer()
                return True
            if action == "intel" and self.access.role(q.from_user.id) == "OWNER":
                await q.message.answer("Open private /settings → Intelligence Desk for all categories and exports.")
                await q.answer()
                return True
            if action == "edit" and self.access.role(q.from_user.id) == "OWNER" and q.bot:
                self.identity_edits[q.from_user.id] = (int(row["target_id"]), time.time()+300)
                try:
                    await q.bot.send_message(q.from_user.id, "Edit identity for "
                        f"<code>{row['target_id']}</code>. Send one field as "
                        "<code>official_first=VALUE</code>, <code>official_last=VALUE</code>, "
                        "<code>friendly=VALUE</code>, <code>birthday=YYYY/MM/DD</code>, "
                        "<code>birthplace=VALUE</code>, or <code>residence=VALUE</code>. "
                        "Send an empty value to clear it. Expires in 5 minutes.")
                except TelegramBadRequest:
                    await q.answer("Open a private chat with Mikachan first")
                    return True
                await q.answer("Edit instructions sent privately")
                return True
            if action == "private" and self.access.role(q.from_user.id) == "OWNER":
                target = int(row["target_id"])
                if target == self.access.primary_owner or not self.access.person(target):
                    await q.answer("Person is not registered")
                    return True
                self.access.set_private(q.from_user.id, target,
                                        not self.access.private_enabled(target))
                action = "manage"
            if action not in {"summary", "details", "manage"} or \
                    action == "manage" and not self.access.can_manage(
                        q.from_user.id, q.message.chat.id):
                await q.answer("Unavailable")
                return True
            body, buttons = self.identity_screen(token, q.message.chat.id,
                int(row["target_id"]), str(row["real_name"]), str(row["username"]),
                action, q.from_user.id)
            await q.message.edit_text(body, reply_markup=buttons)
            await q.answer()
            return True
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
            try:
                await q.message.delete()
            except TelegramBadRequest:
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
