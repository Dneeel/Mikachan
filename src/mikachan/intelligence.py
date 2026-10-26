"""Owner-defined, observed-message keyword ledger for approved groups."""

from __future__ import annotations

import html
import json
import re
import secrets
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .access import AccessRegistry

TEHRAN = ZoneInfo("Asia/Tehran")


class Intelligence:
    def __init__(self, db: sqlite3.Connection, access: AccessRegistry):
        self.db, self.access = db, access
        self.pending: dict[int, tuple[str, str, float]] = {}
        self._patterns: list[tuple[int, re.Pattern[str]]] = []
        self._patterns_at = 0.0
        db.executescript("""
        CREATE TABLE IF NOT EXISTS intel_categories (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, visible INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS intel_terms (
          id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL REFERENCES intel_categories(id),
          phrase TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          UNIQUE(category_id,phrase));
        CREATE TABLE IF NOT EXISTS intel_hits (
          id INTEGER PRIMARY KEY, term_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
          user_id INTEGER NOT NULL, message_id INTEGER NOT NULL, seen REAL NOT NULL,
          excerpt TEXT NOT NULL, occurrence INTEGER NOT NULL,
          UNIQUE(term_id,chat_id,message_id,occurrence));
        CREATE INDEX IF NOT EXISTS intel_hits_term ON intel_hits(term_id,seen);
        CREATE INDEX IF NOT EXISTS intel_hits_person ON intel_hits(user_id,seen);
        CREATE TABLE IF NOT EXISTS intel_profiles (
          user_id INTEGER PRIMARY KEY, official_first TEXT NOT NULL DEFAULT '',
          official_last TEXT NOT NULL DEFAULT '', friendly TEXT NOT NULL DEFAULT '',
          birthday TEXT NOT NULL DEFAULT '', birthplace TEXT NOT NULL DEFAULT '',
          residence TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS intel_sessions (
          token TEXT PRIMARY KEY, actor INTEGER NOT NULL, chat_id INTEGER NOT NULL,
          message_id INTEGER, page TEXT NOT NULL, expires REAL NOT NULL);
        """)

    def add_category(self, name: str) -> int:
        name = " ".join(name.strip().split())
        if not 1 <= len(name) <= 50 or self.db.execute(
                "SELECT COUNT(*) FROM intel_categories").fetchone()[0] >= 30:
            raise ValueError("Category name is invalid or the 30-category limit was reached")
        cursor = self.db.execute("INSERT INTO intel_categories(name) VALUES (?)", (name,))
        return int(cursor.lastrowid or 0)

    def add_term(self, category: int, phrase: str) -> int:
        phrase = " ".join(phrase.strip().casefold().split())
        if not 1 <= len(phrase) <= 80 or self.db.execute(
                "SELECT COUNT(*) FROM intel_terms").fetchone()[0] >= 100:
            raise ValueError("Word is invalid or the 100-word limit was reached")
        if not self.db.execute("SELECT 1 FROM intel_categories WHERE id=?", (category,)).fetchone():
            raise ValueError("Category is unavailable")
        cursor = self.db.execute("INSERT INTO intel_terms(category_id,phrase) VALUES (?,?)",
                                 (category, phrase))
        self._patterns_at = 0
        return int(cursor.lastrowid or 0)

    def observe(self, m: Message) -> None:
        if (m.chat.id not in self.access.groups() or not m.from_user or
                getattr(m.from_user, "is_bot", False) or
                not (m.text or m.caption) or not m.message_id):
            return
        source = str(m.text or m.caption or "")[:1000]
        folded = source.casefold()
        if not self._patterns_at or time.monotonic() - self._patterns_at > 60:
            rows = self.db.execute("SELECT id,phrase FROM intel_terms WHERE active=1").fetchall()
            self._patterns = [(int(term_id), re.compile(r"(?<!\w)" +
                re.escape(str(phrase)).replace(r"\ ", r"\s+") + r"(?!\w)"))
                for term_id, phrase in rows]
            self._patterns_at = time.monotonic()
        for term_id, pattern in self._patterns:
            for index, _match in enumerate(pattern.finditer(folded)):
                self.db.execute("INSERT OR IGNORE INTO intel_hits "
                    "(term_id,chat_id,user_id,message_id,seen,excerpt,occurrence) "
                    "VALUES (?,?,?,?,?,?,?)", (term_id, m.chat.id, m.from_user.id,
                    m.message_id, time.time(), source, index))

    def _keyboard(self, token: str, rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=label, callback_data=f"fi:{token}:{action}")
            for label, action in row] for row in rows])

    def _screen(self, page: str) -> tuple[str, list[list[tuple[str, str]]]]:
        if page == "home":
            return ("🕵️ <b>Mikachan Intelligence Desk</b>\nCounts begin when a word is added. "
                    "Only messages Telegram delivers in approved groups are recorded.",
                    [[("📚 Categories", "categories")], [("👥 People", "people")],
                     [("➕ Add category", "addcat")], [("✖ Close", "close")]])
        if page == "categories":
            rows = self.db.execute("SELECT id,name,visible FROM intel_categories ORDER BY name").fetchall()
            buttons = [[(str(row[1]) + (" · public" if row[2] else " · private"),
                         f"cat{row[0]}")] for row in rows[:30]]
            return "📚 <b>Categories</b>", buttons + [[("◀ Back", "home")]]
        if page.startswith("cat") and page[3:].isdigit():
            ident = int(page[3:])
            row = self.db.execute("SELECT name,visible FROM intel_categories WHERE id=?",
                                  (ident,)).fetchone()
            if row is None:
                return self._screen("categories")
            words = self.db.execute("SELECT t.id,t.phrase,t.active,COUNT(h.id) FROM intel_terms t "
                "LEFT JOIN intel_hits h ON h.term_id=t.id WHERE t.category_id=? "
                "GROUP BY t.id ORDER BY t.phrase", (ident,)).fetchall()
            buttons = [[(f"{r[1]} · {r[3]}", f"term{r[0]}")] for r in words[:100]]
            buttons += [[("➕ Add word", f"addword{ident}"),
                         ("👁 Toggle group visibility", f"visible{ident}")],
                        [("✏️ Rename category", f"renamecat{ident}")],
                        [("📄 TXT", f"ec{ident}t"), ("{} JSON", f"ec{ident}j"),
                         ("🌐 HTML", f"ec{ident}h")],
                        [("⏸ Disable category", f"delcat{ident}")],
                        [("◀ Back", "categories")]]
            return (f"📚 <b>{html.escape(row[0])}</b>\n"
                    f"Group /id visibility · {'On' if row[1] else 'Off'}", buttons)
        if page.startswith("term") and page[4:].isdigit():
            ident = int(page[4:])
            row = self.db.execute("SELECT t.phrase,t.active,c.id,c.name,COUNT(h.id) "
                "FROM intel_terms t JOIN intel_categories c ON c.id=t.category_id "
                "LEFT JOIN intel_hits h ON h.term_id=t.id WHERE t.id=? GROUP BY t.id",
                (ident,)).fetchone()
            if row is None:
                return self._screen("categories")
            top = self.db.execute("SELECT user_id,COUNT(*) n FROM intel_hits WHERE term_id=? "
                "GROUP BY user_id ORDER BY n DESC LIMIT 10", (ident,)).fetchall()
            chats = self.db.execute("SELECT chat_id,COUNT(*) n FROM intel_hits WHERE term_id=? "
                "GROUP BY chat_id ORDER BY n DESC LIMIT 3", (ident,)).fetchall()
            body = (f"🔎 <b>{html.escape(row[0])}</b> · {html.escape(row[3])}\n"
                    f"Uses · {row[4]}\nActive · {'Yes' if row[1] else 'No'}\n"
                    "Top people · " + (", ".join(f"{r[0]} ({r[1]})" for r in top[:3]) or "None") +
                    "\nTop groups · " + (", ".join(f"{r[0]} ({r[1]})" for r in chats) or "None"))
            buttons = [[("🕒 Latest use", f"latest{ident}")],
                       [("📄 TXT", f"export{ident}t"), ("{} JSON", f"export{ident}j"),
                        ("🌐 HTML", f"export{ident}h")],
                       [("✏️ Rename word", f"renameterm{ident}"),
                        ("⏸ Toggle tracking", f"toggle{ident}")],
                       [("📚 Category", f"cat{row[2]}")],
                       [("⏸ Disable word", f"delterm{ident}")]]
            buttons += [[(f"👤 {r[0]} · {r[1]}", f"person{r[0]}")] for r in top]
            return body, buttons + [[("◀ Back", f"cat{row[2]}")]]
        if page == "people" or re.fullmatch(r"people@\d+", page):
            offset = int(page.split("@", 1)[1]) if "@" in page else 0
            rows = self.db.execute("SELECT user_id,COUNT(*) FROM intel_hits GROUP BY user_id "
                                   "ORDER BY COUNT(*) DESC LIMIT 21 OFFSET ?", (offset,)).fetchall()
            navigation = []
            if offset:
                navigation.append(("◀ Previous", f"people@{max(0,offset-20)}"))
            if len(rows) > 20:
                navigation.append(("Next ▶", f"people@{offset+20}"))
            return "👥 <b>Observed people</b>", [[(f"{r[0]} · {r[1]}", f"person{r[0]}")]
                                             for r in rows[:20]] + ([navigation] if navigation else []) + \
                                             [[("◀ Back", "home")]]
        if page.startswith("person") and page[6:].isdigit():
            user = int(page[6:])
            rows = self.db.execute("SELECT c.id,c.name,COUNT(*) FROM intel_hits h "
                "JOIN intel_terms t ON t.id=h.term_id JOIN intel_categories c ON c.id=t.category_id "
                "WHERE h.user_id=? GROUP BY c.id ORDER BY COUNT(*) DESC", (user,)).fetchall()
            return (f"👤 <b>{user}</b>\nObserved words · {sum(r[2] for r in rows)}",
                    [[(f"{r[1]} · {r[2]}", f"pc{user}.{r[0]}")]
                     for r in rows[:30]] + [[("◀ Back", "people")]])
        match = re.fullmatch(r"pc(\d+)\.(\d+)", page)
        if match:
            user, category = map(int, match.groups())
            words = self.db.execute("SELECT t.id,t.phrase,COUNT(*) FROM intel_hits h "
                "JOIN intel_terms t ON t.id=h.term_id WHERE h.user_id=? AND t.category_id=? "
                "GROUP BY t.id ORDER BY COUNT(*) DESC", (user, category)).fetchall()
            name = self.db.execute("SELECT name FROM intel_categories WHERE id=?",
                                   (category,)).fetchone()
            return (f"👤 <b>{user}</b> · {html.escape(name[0]) if name else 'Category'}\n"
                    f"Recorded uses · {sum(r[2] for r in words)}",
                    [[(f"{r[1]} · {r[2]}", f"pt{user}.{r[0]}")] for r in words[:100]] +
                    [[("📄 TXT", f"epc{user}.{category}t"),
                      ("{} JSON", f"epc{user}.{category}j"),
                      ("🌐 HTML", f"epc{user}.{category}h")],
                     [("◀ Back", f"person{user}")]])
        match = re.fullmatch(r"pt(\d+)\.(\d+)", page)
        if match:
            user, term = map(int, match.groups())
            row = self.db.execute("SELECT t.phrase,COUNT(h.id),MAX(h.seen) FROM intel_terms t "
                "LEFT JOIN intel_hits h ON h.term_id=t.id AND h.user_id=? WHERE t.id=?",
                (user, term)).fetchone()
            if not row or not row[0]:
                return self._screen(f"person{user}")
            return (f"👤 <b>{user}</b> · {html.escape(row[0])}\nUses · {row[1]}",
                    [[("🕒 Latest use", f"lpt{user}.{term}")],
                     [("📄 TXT", f"ept{user}.{term}t"),
                      ("{} JSON", f"ept{user}.{term}j"),
                      ("🌐 HTML", f"ept{user}.{term}h")],
                     [("◀ Back", f"person{user}")]])
        return self._screen("home")

    async def open(self, q: CallbackQuery) -> None:
        if not q.from_user or not isinstance(q.message, Message) or q.message.chat.type != "private" or \
                self.access.role(q.from_user.id) != "OWNER":
            await q.answer("Unavailable")
            return
        token = secrets.token_urlsafe(8)
        self.db.execute("INSERT INTO intel_sessions VALUES (?,?,?,?,?,?)",
                        (token, q.from_user.id, q.message.chat.id, None, "home", time.time()+900))
        body, rows = self._screen("home")
        sent = await q.message.answer(body, reply_markup=self._keyboard(token, rows))
        self.db.execute("UPDATE intel_sessions SET message_id=? WHERE token=?",
                        (sent.message_id, token))
        await q.answer()

    async def reply(self, m: Message) -> bool:
        if not m.from_user or m.from_user.id not in self.pending:
            return False
        kind, token, expires = self.pending[m.from_user.id]
        if expires < time.time() or m.chat.type != "private" or \
                self.access.role(m.from_user.id) != "OWNER":
            self.pending.pop(m.from_user.id, None)
            return False
        if not m.text or m.text.startswith("/"):
            return False
        try:
            if kind == "category":
                category = self.add_category(m.text)
                self.pending[m.from_user.id] = (f"word:{category}", token, time.time()+600)
                await m.reply("✅ Category saved. Send its first word or phrase. "
                    "Send more words one at a time; tap Done when finished.",
                    reply_markup=self._batch_keyboard(token))
                return True
            elif kind.startswith("word:"):
                self.add_term(int(kind[5:]), m.text)
                self.pending[m.from_user.id] = (kind, token, time.time()+600)
                await m.reply("✅ Word saved. Send another word or tap Done.",
                              reply_markup=self._batch_keyboard(token))
                return True
            elif kind.startswith("renamecat"):
                value = " ".join(m.text.strip().split())
                if not 1 <= len(value) <= 50:
                    raise ValueError("Category name must be 1–50 characters")
                self.db.execute("UPDATE intel_categories SET name=? WHERE id=?",
                                (value, int(kind[9:])))
            elif kind.startswith("renameterm"):
                value = " ".join(m.text.strip().casefold().split())
                if not 1 <= len(value) <= 80:
                    raise ValueError("Word must be 1–80 characters")
                self.db.execute("UPDATE intel_terms SET phrase=? WHERE id=?",
                                (value, int(kind[10:])))
                self._patterns_at = 0
            else:
                return False
        except (ValueError, sqlite3.IntegrityError) as exc:
            await m.reply("⚠️ " + html.escape(str(exc)))
            return True
        self.pending.pop(m.from_user.id, None)
        await m.reply("✅ Saved. Reopen Intelligence Desk to view it.")
        return True

    @staticmethod
    def _batch_keyboard(token: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Done", callback_data=f"fi_done:{token}")]])

    def export(self, term: int | None, fmt: str, *, category: int | None = None,
               user: int | None = None) -> tuple[bytes, str]:
        where, params = [], []
        if term is not None:
            where.append("h.term_id=?")
            params.append(term)
        if category is not None:
            where.append("t.category_id=?")
            params.append(category)
        if user is not None:
            where.append("h.user_id=?")
            params.append(user)
        rows = self.db.execute("SELECT h.seen,h.chat_id,h.user_id,h.message_id,h.excerpt "
            "FROM intel_hits h JOIN intel_terms t ON t.id=h.term_id WHERE " +
            " AND ".join(where) + " ORDER BY h.seen DESC LIMIT 10000", params).fetchall()
        data = [{"time": datetime.fromtimestamp(r[0], TEHRAN).isoformat(),
                 "chat_id": r[1], "user_id": r[2], "message_id": r[3], "text": r[4]}
                for r in rows]
        if fmt == "j":
            return json.dumps(data, ensure_ascii=False, indent=2).encode(), "json"
        if fmt == "h":
            cards = "".join(
                "<article class='record'><div class='record-head'><span class='stamp'>"
                + html.escape(x["time"]) + "</span><span class='badge'>GROUP "
                + html.escape(str(x["chat_id"])) + "</span></div>"
                + "<h2>SUBJECT " + html.escape(str(x["user_id"])) + "</h2>"
                + "<p class='excerpt' dir='auto'>" + html.escape(x["text"]) + "</p>"
                + "<footer>MESSAGE ID <b>" + html.escape(str(x["message_id"]))
                + "</b></footer></article>" for x in data)
            body = ("<!doctype html><html lang='en'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                "<title>Mikachan Intelligence · Case file</title><style>"
                ":root{color-scheme:dark;font-family:Inter,Arial,sans-serif}*{box-sizing:border-box}"
                "body{margin:0;min-height:100vh;color:#e6edf5;background:radial-gradient(ellipse at 15% 0%,#213c4b 0%,transparent 45%),radial-gradient(ellipse at 95% 85%,#172f34 0%,transparent 40%),#071014}"
                "main{max-width:1100px;margin:auto;padding:clamp(24px,5vw,68px)}"
                ".eyebrow{color:#83dfc3;letter-spacing:.28em;font-size:.72rem;font-weight:700}"
                "h1{font-size:clamp(2rem,5vw,4rem);letter-spacing:-.05em;margin:.55rem 0}"
                ".intro{color:#9eb5be;line-height:1.7;max-width:680px}.grid{display:grid;"
                "grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px;margin-top:36px}"
                ".record,.metric{background:linear-gradient(145deg,#ffffff12,#ffffff05);"
                "border:1px solid #b7f6e127;box-shadow:0 18px 48px #0005;backdrop-filter:blur(18px);"
                "border-radius:18px}.record{padding:22px;min-width:0}.record-head{display:flex;"
                "justify-content:space-between;gap:10px;flex-wrap:wrap}.stamp,.badge,footer{"
                "font-family:ui-monospace,SFMono-Regular,monospace;font-size:.69rem;color:#8faab0}"
                ".badge{border:1px solid #83dfc355;color:#83dfc3;padding:5px 8px;border-radius:5px}"
                "h2{font-size:.82rem;letter-spacing:.12em;margin:22px 0 12px;color:#c5e6df}"
                ".excerpt{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.65;"
                "min-height:3em;margin:0;color:#edf6f4}footer{border-top:1px solid #ffffff19;"
                "margin-top:24px;padding-top:14px}.metric{display:inline-block;padding:14px 20px;"
                "margin-top:18px;color:#83dfc3}small{display:block;margin-top:35px;color:#78959b}"
                "</style><main><div class='eyebrow'>MIKACHAN / INTELLIGENCE DESK</div>"
                "<h1>Case file</h1><p class='intro'>A record of observed messages in approved "
                "groups. Counts begin when tracking is enabled. This export is private.</p>"
                f"<div class='metric'>{len(data):,} RECORDED USES</div>"
                "<section class='grid'>" + cards + "</section>"
                "<small>END OF FILE · Local export · Asia/Tehran</small></main></html>")
            return body.encode(), "html"
        return "\n\n".join(f"{x['time']} · {x['user_id']} · {x['chat_id']}\n{x['text']}"
                          for x in data).encode(), "txt"

    async def callback(self, q: CallbackQuery, data: str) -> bool:
        if data.startswith("fi_done:"):
            pending = self.pending.get(q.from_user.id) if q.from_user else None
            if not q.from_user or not isinstance(q.message, Message) or \
                    q.message.chat.type != "private" or \
                    self.access.role(q.from_user.id) != "OWNER" or not pending or \
                    not pending[0].startswith("word:") or pending[1] != data[8:] or \
                    pending[2] < time.time():
                await q.answer("Unavailable")
                return True
            self.pending.pop(q.from_user.id, None)
            await q.message.edit_text("✅ Category words saved.")
            await q.answer()
            return True
        if not data.startswith("fi:"):
            return False
        if not q.from_user or not isinstance(q.message, Message):
            return True
        parts = data.split(":", 2)
        if len(parts) != 3:
            await q.answer()
            return True
        token, action = parts[1:]
        row = self.db.execute("SELECT 1 FROM intel_sessions WHERE token=? AND actor=? "
            "AND chat_id=? AND message_id=? AND expires>?", (token, q.from_user.id,
            q.message.chat.id, q.message.message_id, time.time())).fetchone()
        if not row or q.message.chat.type != "private" or \
                self.access.role(q.from_user.id) != "OWNER":
            await q.answer("Unavailable")
            return True
        if action == "close":
            await q.message.delete()
            return True
        if action == "addcat" or action.startswith(("addword", "renamecat", "renameterm")):
            kind = "category" if action == "addcat" else \
                "word:" + action[7:] if action.startswith("addword") else action
            self.pending[q.from_user.id] = (kind, token, time.time()+600)
            await q.message.answer("Send the new category name." if kind in {"category"} or
                kind.startswith("renamecat") else "Send the first word or phrase. "
                "Continue sending words one at a time; tap Done when finished.",
                reply_markup=self._batch_keyboard(token) if kind.startswith("word:") else None)
            await q.answer()
            return True
        if action.startswith("visible") or action.startswith("toggle"):
            table, field, ident = ("intel_categories", "visible", action[7:]) if \
                action.startswith("visible") else ("intel_terms", "active", action[6:])
            if ident.isdigit():
                self.db.execute(f"UPDATE {table} SET {field}=1-{field} WHERE id=?", (int(ident),))
                self._patterns_at = 0
            action = ("cat" if table == "intel_categories" else "term") + ident
        if action.startswith("delterm") and action[7:].isdigit():
            term_id = int(action[7:])
            self.db.execute("UPDATE intel_terms SET active=0 WHERE id=?", (term_id,))
            self._patterns_at = 0
            action = "term" + str(term_id)
        if action.startswith("delcat") and action[6:].isdigit():
            category_id = int(action[6:])
            self.db.execute("UPDATE intel_terms SET active=0 WHERE category_id=?", (category_id,))
            self._patterns_at = 0
            self.db.execute("UPDATE intel_categories SET visible=0 WHERE id=?", (category_id,))
            action = "categories"
        if action.startswith("latest") and action[6:].isdigit():
            row = self.db.execute("SELECT seen,user_id,excerpt FROM intel_hits WHERE term_id=? "
                "ORDER BY seen DESC LIMIT 1", (int(action[6:]),)).fetchone()
            await q.message.answer((f"{datetime.fromtimestamp(row[0], TEHRAN):%Y/%m/%d %H:%M}\n"
                f"{row[1]}:\n{html.escape(row[2])}") if row else "No recorded use yet.")
            await q.answer()
            return True
        pair = re.fullmatch(r"lpt(\d+)\.(\d+)", action)
        if pair:
            user, term = map(int, pair.groups())
            latest = self.db.execute("SELECT seen,excerpt FROM intel_hits WHERE user_id=? "
                "AND term_id=? ORDER BY seen DESC LIMIT 1", (user, term)).fetchone()
            await q.message.answer((f"{datetime.fromtimestamp(latest[0], TEHRAN):%Y/%m/%d %H:%M}\n"
                f"{user}:\n{html.escape(latest[1])}") if latest else "No recorded use yet.")
            await q.answer()
            return True
        selected = re.fullmatch(r"e(pc|pt|c)(\d+)(?:\.(\d+))?([tjh])", action)
        if selected:
            scope, first, second, fmt = selected.groups()
            export_user = int(first) if scope in {"pc", "pt"} else None
            export_category = int(second) if scope == "pc" and second else \
                int(first) if scope == "c" else None
            export_term = int(second) if scope == "pt" and second else None
            payload, ext = self.export(export_term, fmt, category=export_category,
                                       user=export_user)
            await q.message.answer_document(BufferedInputFile(payload, f"observed-{scope}.{ext}"))
            await q.answer()
            return True
        if action.startswith("export") and action[-1:] in {"t", "j", "h"} and \
                action[6:-1].isdigit():
            export_id = int(action[6:-1])
            payload, ext = self.export(export_id, action[-1])
            await q.message.answer_document(BufferedInputFile(payload, f"observed-{export_id}.{ext}"))
            await q.answer()
            return True
        body, buttons = self._screen(action)
        self.db.execute("UPDATE intel_sessions SET page=? WHERE token=?", (action, token))
        await q.message.edit_text(body, reply_markup=self._keyboard(token, buttons))
        await q.answer()
        return True
