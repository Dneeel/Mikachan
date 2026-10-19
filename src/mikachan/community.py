"""Small group features and reviewed cultural content within the main bot dispatcher."""

import html
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path

import httpx

from aiogram import Bot
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .ganjoor import Ganjoor, render_poem
from .access import AccessRegistry

PROTECTED = {"start", "help", "search", "music", "podcast", "pod", "twitter", "instagram",
             "tiktok", "youtube", "queue", "cancel", "status", "settings", "admin", "fal",
             "poem", "joke", "quote", "dice", "nickname", "rules", "setrules", "note",
             "setnote", "delnote", "setcmd", "delcmd", "welcome", "del", "pin", "mute",
             "unmute", "ban", "unban", "bans",
             "modlog", "groupstats", "groupstatus", "groupsettings", "qr", "scan", "ocr", "image",
             "sticker", "stickerimage", "gif", "pack", "convert", "weather", "rate",
             "time", "apod", "translate"}
NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
SCHEMA = """
CREATE TABLE IF NOT EXISTS group_settings (
 chat_id INTEGER NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(chat_id,name));
CREATE TABLE IF NOT EXISTS group_notes (
 chat_id INTEGER NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(chat_id,name));
CREATE TABLE IF NOT EXISTS group_commands (
 chat_id INTEGER NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(chat_id,name));
CREATE TABLE IF NOT EXISTS group_nicknames (
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, nickname TEXT NOT NULL,
 PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS community_audit (
 id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, actor_id INTEGER NOT NULL,
 action TEXT NOT NULL, target_id INTEGER, result TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS feature_usage (
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, feature TEXT NOT NULL,
 day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(chat_id,user_id,feature,day));
CREATE TABLE IF NOT EXISTS content_items (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, language TEXT NOT NULL,
 body TEXT NOT NULL, author TEXT NOT NULL, source TEXT NOT NULL,
 rights TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS community_sessions (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 message_id INTEGER, kind TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cultural_seed (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS managed_bans (
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, actor_id INTEGER NOT NULL,
 created REAL NOT NULL, active INTEGER NOT NULL, PRIMARY KEY(chat_id,user_id));
"""


class CommunityStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.executescript(SCHEMA)

    def setting(self, chat: int, name: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM group_settings WHERE chat_id=? AND name=?",
                              (chat, name)).fetchone()
        return str(row[0]) if row else default

    def set_setting(self, chat: int, name: str, value: str) -> None:
        self.db.execute("INSERT INTO group_settings VALUES (?,?,?) ON CONFLICT(chat_id,name) "
                        "DO UPDATE SET value=excluded.value", (chat, name, value))

    def enabled(self, chat: int, domain: str) -> bool:
        return self.setting(chat, "module_" + domain, "1") == "1"

    def item(self, table: str, chat: int, name: str) -> str | None:
        if table not in {"group_notes", "group_commands"}:
            raise ValueError("invalid table")
        row = self.db.execute(f"SELECT body FROM {table} WHERE chat_id=? AND name=?",
                              (chat, name)).fetchone()
        return str(row[0]) if row else None

    def set_item(self, table: str, chat: int, name: str, body: str | None) -> None:
        if table not in {"group_notes", "group_commands"}:
            raise ValueError("invalid table")
        if body is None:
            self.db.execute(f"DELETE FROM {table} WHERE chat_id=? AND name=?", (chat, name))
        else:
            self.db.execute(f"INSERT INTO {table} VALUES (?,?,?) ON CONFLICT(chat_id,name) "
                            "DO UPDATE SET body=excluded.body", (chat, name, body))

    def nickname(self, chat: int, user: int) -> str | None:
        row = self.db.execute("SELECT nickname FROM group_nicknames WHERE chat_id=? AND user_id=?",
                              (chat, user)).fetchone()
        return str(row[0]) if row else None

    def set_nickname(self, chat: int, user: int, name: str | None) -> None:
        if name is None:
            self.db.execute("DELETE FROM group_nicknames WHERE chat_id=? AND user_id=?",
                            (chat, user))
        else:
            self.db.execute("INSERT INTO group_nicknames VALUES (?,?,?) ON CONFLICT(chat_id,user_id) "
                            "DO UPDATE SET nickname=excluded.nickname", (chat, user, name))

    def audit(self, chat: int, actor: int, action: str, target: int | None, result: str) -> None:
        self.db.execute("INSERT INTO community_audit(chat_id,actor_id,action,target_id,result,created) "
                        "VALUES (?,?,?,?,?,?)", (chat, actor, action, target, result, time.time()))

    def use(self, chat: int, user: int, feature: str) -> None:
        self.db.execute("INSERT INTO feature_usage VALUES (?,?,?,date('now'),1) "
                        "ON CONFLICT(chat_id,user_id,feature,day) DO UPDATE SET count=count+1",
                        (chat, user, feature))

    def import_content(self, path: Path) -> int:
        """Owner review/import: explicit rights and provenance; never auto-populate."""
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("content import exceeds 8 MiB")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or len(data) > 5000:
            raise ValueError("content import must be a JSON list of at most 5000 items")
        rows = []
        for entry in data:
            if not isinstance(entry, dict) or entry.get("kind") not in {"poem", "quote"}:
                raise ValueError("invalid content kind")
            values = tuple(str(entry.get(k, "")).strip() for k in
                           ("kind", "language", "body", "author", "source", "rights"))
            if any(not x for x in values) or len(values[2]) > 12000 or len(values[4]) > 500:
                raise ValueError("content needs language, body, author, source and rights")
            rows.append(values)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.executemany("INSERT INTO content_items(kind,language,body,author,source,rights,approved) "
                                "VALUES (?,?,?,?,?,?,1)", rows)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return len(rows)

    def seed_hafez(self, path: Path) -> int:
        """Install reviewed bundled Wikisource texts once, preserving owner imports."""
        if self.db.execute("SELECT 1 FROM cultural_seed WHERE name='hafez-wikisource-1'").fetchone():
            return 0
        entries = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entries, list) or not 0 < len(entries) <= 50:
            raise ValueError("Invalid bundled Hafez catalog")
        added = 0
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("kind") != "poem" or \
                        entry.get("author") != "Hafez" or \
                        not str(entry.get("source", "")).startswith("https://fa.wikisource.org/wiki/") or \
                        not entry.get("rights") or not entry.get("body"):
                    raise ValueError("Invalid bundled Hafez entry")
                if self.db.execute("SELECT 1 FROM content_items WHERE source=? LIMIT 1",
                                   (entry["source"],)).fetchone():
                    continue
                self.db.execute("INSERT INTO content_items(kind,language,body,author,source,rights,approved) "
                    "VALUES (?,?,?,?,?,?,1)", ("poem", "fa", entry["body"], "Hafez",
                                            entry["source"], entry["rights"]))
                added += 1
            self.db.execute("INSERT INTO cultural_seed VALUES ('hafez-wikisource-1')")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return added

    def content(self, kind: str, author: str | None = None, query: str | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM content_items WHERE approved=1 AND kind=?"
        args: list[str] = [kind]
        if author:
            sql += " AND author=?"
            args.append(author)
        if query:
            sql += " AND body LIKE ?"
            args.append("%" + query.replace("%", "\\%").replace("_", "\\_") + "%")
            sql += " ESCAPE '\\'"
        return self.db.execute(sql + " ORDER BY RANDOM() LIMIT 1", args).fetchone()

    def poets(self) -> list[str]:
        return [str(r[0]) for r in self.db.execute("SELECT DISTINCT author FROM content_items "
                "WHERE approved=1 AND kind='poem' ORDER BY author LIMIT 20")]

    def hafez(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM content_items WHERE approved=1 AND kind='poem' "
            "AND author IN ('Hafez','Hafez Shirazi','حافظ','حافظ شیرازی') "
            "ORDER BY RANDOM() LIMIT 1").fetchone()

    def new_session(self, user: int, chat: int, kind: str) -> str:
        sid = secrets.token_urlsafe(9)
        self.db.execute("INSERT INTO community_sessions VALUES (?,?,?,NULL,?,?)",
                        (sid, user, chat, kind, time.time() + 600))
        return sid

    def session(self, sid: str, user: int, chat: int, message: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM community_sessions WHERE id=? AND user_id=? "
                               "AND chat_id=? AND message_id=? AND expires>?",
                               (sid, user, chat, message, time.time())).fetchone()


class Community:
    def __init__(self, bot: Bot, store: CommunityStore, owner_id: int,
                 access: AccessRegistry | None = None):
        self.bot, self.store, self.owner_id = bot, store, owner_id
        self.access = access
        self.ganjoor = Ganjoor()

    async def admin(self, chat: int, user: int) -> bool:
        return self.access.can_manage(user, chat) if self.access else user == self.owner_id

    async def welcome(self, m: Message, allowed_users: set[int]) -> None:
        if not m.new_chat_members or self.store.setting(m.chat.id, "welcome_enabled") != "1":
            return
        template = self.store.setting(m.chat.id, "welcome_text", "Welcome, {name}.")
        for user in m.new_chat_members:
            if user.id in allowed_users:
                name = self.store.nickname(m.chat.id, user.id) or user.full_name
                await m.answer(html.escape(template.replace("{name}", name)))

    async def command(self, m: Message, cmd: str, arg: str) -> bool:
        if not m.from_user:
            return False
        chat, user = m.chat.id, m.from_user.id
        group = m.chat.type in {"group", "supergroup"}
        if group and cmd in {"/fal", "/poem", "/quote", "/dice"} and not self.store.enabled(chat, "culture"):
            return True
        if group and cmd in {"/rules", "/note"} and not self.store.enabled(chat, "notes"):
            return True
        if cmd in {"/fal", "/poem", "/quote"}:
            self.store.use(chat, user, cmd[1:])
            if cmd == "/poem":
                sid = self.store.new_session(user, chat, "poem")
                markup = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Random poem", callback_data=f"u:{sid}:random")],
                    [InlineKeyboardButton(text="Choose poet", callback_data=f"u:{sid}:poets")],
                    [InlineKeyboardButton(text="Search poetry", callback_data=f"u:{sid}:search")],
                    [InlineKeyboardButton(text="✖ Close", callback_data=f"u:{sid}:close")],
                ])
                sent = await m.reply("📜 <b>Poetry</b>\nBrowse Ganjoor's public catalog or search for a verse.",
                                     reply_markup=markup)
                self.store.db.execute("UPDATE community_sessions SET message_id=? WHERE id=?",
                                      (sent.message_id, sid))
            else:
                row = self.store.hafez() if cmd == "/fal" else self.store.content(cmd[1:])
                await m.reply(self.render_content(row) if row else
                              "No reviewed content is installed for this feature yet.")
            return True
        if cmd == "/dice":
            self.store.use(chat, user, "dice")
            await self.bot.send_dice(chat, emoji="🎲")
            return True
        if not group:
            return False
        if cmd == "/rules":
            self.store.use(chat, user, "rules")
            await m.reply(html.escape(self.store.setting(chat, "rules") or "No rules have been set."))
            return True
        if cmd == "/note":
            value = self.store.item("group_notes", chat, arg.lower()) if NAME.fullmatch(arg.lower()) else None
            await m.reply(html.escape(value) if value else "Note not found. Use /note NAME.")
            self.store.use(chat, user, "note")
            return True
        if cmd == "/groupstats":
            if arg == "user":
                target = m.reply_to_message.from_user if m.reply_to_message else None
                if not target or not await self.admin(chat, user):
                    return True
                row = self.store.db.execute("SELECT COALESCE(SUM(count),0) FROM feature_usage "
                    "WHERE chat_id=? AND user_id=? AND feature='request' "
                    "AND day>=date('now','-29 days')", (chat, target.id)).fetchone()
                await m.reply(f"Observed authorized message requests from user {target.id} "
                              f"in this group, last 30 UTC days: {row[0]}.")
                return True
            row = self.store.db.execute("SELECT COALESCE(SUM(count),0), COUNT(DISTINCT user_id) "
                "FROM feature_usage WHERE chat_id=? AND feature='request' "
                "AND day>=date('now','-29 days')", (chat,)).fetchone()
            await m.reply(f"Observed authorized message requests to Mikachan in this group, "
                          f"last 30 UTC days: {row[0]}. Distinct requesters: {row[1]}. "
                          "Button actions and ordinary conversation are not counted.")
            return True
        if cmd == "/groupsettings":
            if not await self.admin(chat, user):
                return True
            parts = arg.lower().split()
            if len(parts) == 2 and parts[0] in {"culture", "notes", "utilities",
                                                  "downloader", "follow"} and parts[1] in {"on", "off"}:
                if await self.admin(chat, user):
                    ceiling = parts[0] if parts[0] in {"utilities", "downloader", "follow"} \
                        else "community"
                    if parts[1] == "on" and self.access and not self.access.module_enabled(ceiling):
                        await m.reply("Global policy disables this module. The Owner must enable it first.")
                        return True
                    self.store.set_setting(chat, "module_" + parts[0], "1" if parts[1] == "on" else "0")
                    self.store.audit(chat, user, "module_" + parts[0], None, parts[1])
                    await m.reply("Group module setting saved.")
                return True
            await self.group_panel(m)
            return True
        if cmd in {"/setrules", "/setnote", "/delnote", "/setcmd", "/delcmd", "/welcome",
                   "/nickname", "/del", "/pin", "/mute", "/unmute", "/ban",
                   "/unban", "/bans", "/modlog"}:
            if not await self.admin(chat, user):
                return True
            if cmd == "/setrules" and 0 < len(arg) <= 3500:
                self.store.set_setting(chat, "rules", arg)
                self.store.audit(chat, user, "rules", None, "updated")
                await m.reply("Rules saved.")
            elif cmd in {"/setnote", "/setcmd"}:
                name, _, body = arg.partition(" ")
                name = name.lower()
                if NAME.fullmatch(name) and 0 < len(body) <= 3000 and (cmd != "/setcmd" or name not in PROTECTED):
                    self.store.set_item("group_notes" if cmd == "/setnote" else "group_commands",
                                        chat, name, body)
                    self.store.audit(chat, user, cmd[1:], None, "updated")
                    await m.reply("Saved.")
                else:
                    await m.reply("Use a unique name and text (up to 3000 characters).")
            elif cmd in {"/delnote", "/delcmd"} and NAME.fullmatch(arg.lower()):
                self.store.set_item("group_notes" if cmd == "/delnote" else "group_commands",
                                    chat, arg.lower(), None)
                self.store.audit(chat, user, cmd[1:], None, "removed")
                await m.reply("Removed.")
            elif cmd == "/welcome":
                if arg in {"on", "off"}:
                    self.store.set_setting(chat, "welcome_enabled", "1" if arg == "on" else "0")
                elif 0 < len(arg) <= 500:
                    self.store.set_setting(chat, "welcome_text", arg)
                else:
                    await m.reply("Use /welcome on, /welcome off, or /welcome TEXT with {name}.")
                    return True
                self.store.audit(chat, user, "welcome", None, "updated")
                await m.reply("Welcome setting saved.")
            elif cmd == "/nickname":
                target = m.reply_to_message.from_user if m.reply_to_message else m.from_user
                if target and 0 < len(arg) <= 64:
                    self.store.set_nickname(chat, target.id, arg)
                    self.store.audit(chat, user, "nickname", target.id, "updated")
                    await m.reply("Group nickname saved.")
                elif target and arg == "":
                    self.store.set_nickname(chat, target.id, None)
                    self.store.audit(chat, user, "nickname", target.id, "removed")
                    await m.reply("Group nickname removed.")
            elif cmd == "/bans":
                rows = self.store.db.execute("SELECT user_id,actor_id,created FROM managed_bans "
                    "WHERE chat_id=? AND active=1 ORDER BY created DESC LIMIT 20", (chat,)).fetchall()
                await m.reply("🚫 <b>Managed Bans</b>\n" + ("\n".join(
                    f"<code>{r['user_id']}</code> · by {r['actor_id']} · "
                    f"{time.strftime('%Y-%m-%d', time.gmtime(r['created']))}" for r in rows)
                    or "No active bans recorded by Mikachan.") +
                    "\nOnly bans managed by Mikachan are listed.")
            elif cmd in {"/del", "/pin", "/mute", "/unmute", "/ban", "/unban"}:
                target_msg = m.reply_to_message
                target = target_msg.from_user if target_msg else None
                if cmd in {"/del", "/pin"}:
                    if not target_msg:
                        await m.reply(f"Reply to the message you want to {cmd[1:]}.")
                        return True
                    target_id = target.id if target else 0
                    protected_person = self.access.person(target_id) if self.access else None
                    if cmd == "/del" and (target_id == self.owner_id or
                            protected_person is not None and
                            protected_person["role"] in {"OWNER", "ADMIN"}):
                        await m.reply("Protected Owner and Admin messages cannot be deleted here.")
                        return True
                else:
                    args = arg.split()
                    duration = 10
                    if target_msg and target:
                        target_id = target.id
                        if cmd == "/mute" and args:
                            if len(args) != 1 or not args[0].isdigit():
                                await m.reply("Reply with /mute MINUTES, for example /mute 67.")
                                return True
                            duration = int(args[0])
                        elif args:
                            await m.reply(f"Reply with bare {cmd}.")
                            return True
                    else:
                        if (not args or not args[0].isdigit() or
                                cmd == "/mute" and (len(args) != 2 or not args[1].isdigit()) or
                                cmd != "/mute" and len(args) != 1):
                            await m.reply(f"Reply to a user or use {cmd} USER_ID" +
                                          (" MINUTES." if cmd == "/mute" else "."))
                            return True
                        target_id = int(args[0])
                        if cmd == "/mute":
                            duration = int(args[1])
                        known = self.store.db.execute("SELECT 1 FROM known_group_members "
                            "WHERE chat_id=? AND user_id=?", (chat, target_id)).fetchone()
                        registered = self.access.person(target_id) if self.access else None
                        if not known and not registered and cmd != "/unban":
                            await m.reply("Target ID has not been observed or registered.")
                            return True
                    if not 0 < target_id < 2**63 or cmd == "/mute" and not 1 <= duration <= 10080:
                        await m.reply("Invalid target or duration; mute range is 1–10080 minutes.")
                        return True
                    protected_person = self.access.person(target_id) if self.access else None
                    if (target_id == self.owner_id or protected_person is not None and
                            protected_person["role"] in {"OWNER", "ADMIN"} or
                            await self.admin(chat, target_id)):
                        await m.reply("Protected Owner and Admin accounts cannot be moderated here.")
                        return True
                    if cmd == "/ban":
                        sid = self.store.new_session(user, chat, f"ban:{target_id}")
                        sent = await m.reply(f"Ban user <code>{target_id}</code> from this group?",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="✅ Ban", callback_data=f"g:{sid}:ban_confirm"),
                                InlineKeyboardButton(text="❌ Cancel", callback_data=f"g:{sid}:ban_cancel")]]))
                        self.store.db.execute("UPDATE community_sessions SET message_id=? WHERE id=?",
                                              (sent.message_id, sid))
                        return True
                try:
                    if cmd == "/del":
                        assert target_msg is not None
                        await self.bot.delete_message(chat, target_msg.message_id)
                    elif cmd == "/pin":
                        assert target_msg is not None
                        await self.bot.pin_chat_message(chat, target_msg.message_id,
                                                        disable_notification=True)
                    elif cmd == "/mute":
                        from aiogram.types import ChatPermissions
                        await self.bot.restrict_chat_member(chat, target_id,
                            ChatPermissions(can_send_messages=False),
                            until_date=int(time.time()) + duration * 60)
                    elif cmd == "/unmute":
                        from aiogram.types import ChatPermissions
                        await self.bot.restrict_chat_member(chat, target_id,
                            ChatPermissions(can_send_messages=True, can_send_audios=True,
                                can_send_documents=True, can_send_photos=True,
                                can_send_videos=True, can_send_video_notes=True,
                                can_send_voice_notes=True, can_send_polls=True,
                                can_send_other_messages=True,
                                can_add_web_page_previews=True))
                    elif cmd == "/unban":
                        await self.bot.unban_chat_member(chat, target_id, only_if_banned=True)
                        self.store.db.execute("UPDATE managed_bans SET active=0 WHERE chat_id=? "
                                              "AND user_id=?", (chat, target_id))
                    result = "completed"
                except Exception as exc:
                    result = f"failed: {type(exc).__name__}"
                self.store.audit(chat, user, cmd[1:], target.id if cmd in {"/del", "/pin"}
                                 and target else target_id, result)
                await m.reply(html.escape(result))
            elif cmd == "/modlog":
                audit_rows = self.store.db.execute("SELECT actor_id,action,target_id,result,created "
                    "FROM community_audit WHERE chat_id=? ORDER BY id DESC LIMIT 10", (chat,))
                lines = [f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(r[4]))} · "
                         f"{r[0]} · {r[1]} · {r[2] or '-'} · {r[3]}" for r in audit_rows]
                await m.reply(html.escape("\n".join(lines) or "No Mikachan actions recorded."))
            return True
        name = cmd.lstrip("/")
        if NAME.fullmatch(name):
            custom_body = self.store.item("group_commands", chat, name)
            if custom_body:
                self.store.use(chat, user, "custom_command")
                await m.reply(html.escape(custom_body))
                return True
        return False

    @staticmethod
    def render_content(row: sqlite3.Row) -> str:
        return ("📜 <b>" + html.escape(str(row["author"])) + "</b>\n\n" +
                html.escape(str(row["body"])[:3400]))

    def _group_view(self, chat: int, sid: str, section: str) -> tuple[str, InlineKeyboardMarkup]:
        def button(label: str, action: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(text=label, callback_data=f"g:{sid}:{action}")
        if section == "modules":
            rows = [[button(f"{'✅' if self.store.enabled(chat, key) else '⬜'} {key.title()}",
                            f"toggle_{key}")]
                    for key in ("downloader", "follow", "culture", "notes", "utilities")]
            body = "🧩 <b>Group Modules</b>\nChoose a module to enable or disable in this group."
        elif section == "welcome":
            enabled = self.store.setting(chat, "welcome_enabled") == "1"
            rows = [[button("Disable welcome" if enabled else "Enable welcome", "welcome_toggle")]]
            body = (f"👋 <b>Welcome</b> · {'On' if enabled else 'Off'}\n"
                    f"Current text · {html.escape(self.store.setting(chat, 'welcome_text', 'Welcome, {name}.')[:300])}\n"
                    "Use /welcome TEXT to change it. Only approved people receive a welcome.")
        elif section == "rules":
            rows = []
            body = ("📜 <b>Group Rules</b>\n" +
                    html.escape(self.store.setting(chat, "rules")[:3000] or "No rules have been set.") +
                    "\nUse /setrules TEXT to update.")
        elif section == "stats":
            enabled = self.store.setting(chat, "groupstatus_user_enabled", "1") == "1"
            rows = [[button("Disable User view" if enabled else "Enable User view", "stats_toggle")]]
            body = (f"📊 <b>Group Statistics</b> · User view {'On' if enabled else 'Off'}\n"
                    "Owners and assigned Admins can always use /groupstatus. "
                    "Only messages Telegram delivers to Mikachan are counted.")
        elif section == "logs":
            logs = self.store.db.execute("SELECT actor_id,action,target_id,result FROM community_audit "
                "WHERE chat_id=? ORDER BY id DESC LIMIT 8", (chat,)).fetchall()
            rows = []
            body = "📋 <b>Group Activity</b>\n" + ("\n".join(html.escape(
                f"{r['actor_id']} · {r['action']} · {r['target_id'] or '-'} · {r['result']}")
                for r in logs) or "No Mikachan settings or moderation actions recorded.")
        else:
            rows = [[button("👋 Welcome Studio", "welcome")],
                    [button("📜 Rules", "rules")],
                    [button("🧩 Modules", "modules")],
                    [button("📊 Group Statistics", "stats")],
                    [button("📋 Activity & Logs", "logs")]]
            body = "⚙️ <b>Group Settings</b>\nManage this approved group."
        rows.append([button("◀ Back", "home")] if section != "home" else
                    [button("✖ Close", "close")])
        return body, InlineKeyboardMarkup(inline_keyboard=rows)

    async def group_panel(self, m: Message) -> None:
        if not m.from_user or not await self.admin(m.chat.id, m.from_user.id):
            return
        sid = self.store.new_session(m.from_user.id, m.chat.id, "group")
        body, buttons = self._group_view(m.chat.id, sid, "home")
        sent = await m.reply(body, reply_markup=buttons)
        self.store.db.execute("UPDATE community_sessions SET message_id=? WHERE id=?",
                              (sent.message_id, sid))

    async def group_callback(self, q: CallbackQuery, data: str) -> bool:
        if not data.startswith("g:") or not isinstance(q.message, Message) or not q.from_user:
            return False
        parts = data.split(":")
        if len(parts) != 3:
            await q.answer()
            return True
        sid, action = parts[1:]
        session = self.store.session(sid, q.from_user.id, q.message.chat.id,
                                     q.message.message_id)
        if not session or not await self.admin(
                q.message.chat.id, q.from_user.id):
            await q.answer("Group control is unavailable")
            return True
        if str(session["kind"]).startswith("ban:"):
            if action not in {"ban_confirm", "ban_cancel"}:
                await q.answer()
                return True
            target_id = int(str(session["kind"]).split(":", 1)[1])
            self.store.db.execute("DELETE FROM community_sessions WHERE id=?", (sid,))
            await q.answer()
            if action == "ban_cancel":
                await q.message.edit_text("Ban cancelled.")
                return True
            protected = self.access.person(target_id) if self.access else None
            if (target_id == self.owner_id or protected and protected["role"] in {"OWNER", "ADMIN"}):
                await q.message.edit_text("Protected account; ban denied.")
                return True
            try:
                await self.bot.ban_chat_member(q.message.chat.id, target_id)
                self.store.db.execute("INSERT INTO managed_bans VALUES (?,?,?,?,1) "
                    "ON CONFLICT(chat_id,user_id) DO UPDATE SET actor_id=excluded.actor_id,"
                    "created=excluded.created,active=1", (q.message.chat.id, target_id,
                    q.from_user.id, time.time()))
                result = "completed"
            except Exception as exc:
                result = f"failed: {type(exc).__name__}"
            self.store.audit(q.message.chat.id, q.from_user.id, "ban", target_id, result)
            await q.message.edit_text(f"Ban {html.escape(result)}.")
            return True
        if session["kind"] != "group":
            await q.answer("Group control is unavailable")
            return True
        await q.answer()
        if action == "close":
            self.store.db.execute("DELETE FROM community_sessions WHERE id=?", (sid,))
            await q.message.edit_reply_markup(reply_markup=None)
            return True
        if action.startswith("toggle_") and action[7:] in {"downloader", "follow", "culture",
                                                           "notes", "utilities"}:
            domain = action[7:]
            new = "0" if self.store.enabled(q.message.chat.id, domain) else "1"
            ceiling = domain if domain in {"utilities", "downloader", "follow"} else "community"
            if new == "1" and self.access and not self.access.module_enabled(ceiling):
                await q.message.answer("The global Owner policy disables this module.")
                return True
            self.store.set_setting(q.message.chat.id, "module_" + domain, new)
            self.store.audit(q.message.chat.id, q.from_user.id, "module_" + domain,
                             None, "on" if new == "1" else "off")
            action = "modules"
        elif action == "welcome_toggle":
            new = "0" if self.store.setting(q.message.chat.id, "welcome_enabled") == "1" else "1"
            self.store.set_setting(q.message.chat.id, "welcome_enabled", new)
            self.store.audit(q.message.chat.id, q.from_user.id, "welcome", None,
                             "on" if new == "1" else "off")
            action = "welcome"
        elif action == "stats_toggle":
            new = "0" if self.store.setting(q.message.chat.id,
                "groupstatus_user_enabled", "1") == "1" else "1"
            self.store.set_setting(q.message.chat.id, "groupstatus_user_enabled", new)
            self.store.audit(q.message.chat.id, q.from_user.id, "groupstatus_user", None,
                             "on" if new == "1" else "off")
            action = "stats"
        if action not in {"home", "modules", "welcome", "rules", "stats", "logs"}:
            return True
        body, buttons = self._group_view(q.message.chat.id, sid, action)
        await q.message.edit_text(body, reply_markup=buttons)
        return True

    async def callback(self, q: CallbackQuery, data: str) -> bool:
        if data.startswith("g:"):
            return await self.group_callback(q, data)
        if not data.startswith("u:") or not q.message or not q.from_user:
            return False
        parts = data.split(":")
        if len(parts) < 3:
            return True
        sid, action = parts[1:3]
        session = self.store.session(sid, q.from_user.id, q.message.chat.id, q.message.message_id)
        if not session:
            await q.answer("This menu is unavailable.")
            return True
        if action == "close":
            self.store.db.execute("DELETE FROM community_sessions WHERE id=?", (sid,))
            await q.answer()
            await self.bot.edit_message_text("Poetry menu closed.",
                                             chat_id=q.message.chat.id,
                                             message_id=q.message.message_id)
        elif action == "random":
            await q.answer()
            try:
                await q.message.answer(render_poem(await self.ganjoor.random()))
            except (ValueError, httpx.HTTPError):
                row = self.store.content("poem")
                await q.message.answer(self.render_content(row) if row else
                                       "Poetry provider is unavailable. Try again later.")
        elif action == "poets":
            await q.answer()
            try:
                poets = await self.ganjoor.poets()
                page = int(parts[3]) if len(parts) == 4 and parts[3].isdigit() else 0
                if page < 0 or page >= (len(poets) + 11) // 12:
                    raise ValueError("Invalid poet page")
                controls = []
                if page:
                    controls.append(InlineKeyboardButton(text="◀ Previous", callback_data=f"u:{sid}:poets:{page-1}"))
                if (page + 1) * 12 < len(poets):
                    controls.append(InlineKeyboardButton(text="Next ▶", callback_data=f"u:{sid}:poets:{page+1}"))
                rows = [[InlineKeyboardButton(text=name, callback_data=f"u:{sid}:p:{pid}")]
                        for pid, name in poets[page*12:(page+1)*12]]
                if controls:
                    rows.append(controls)
                rows.append([InlineKeyboardButton(text="✖ Close", callback_data=f"u:{sid}:close")])
                await self.bot.edit_message_text(f"📜 <b>Choose a poet</b> · {page+1}/{(len(poets)+11)//12}",
                    chat_id=q.message.chat.id, message_id=q.message.message_id,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
            except (ValueError, httpx.HTTPError):
                await q.message.answer("Poet catalog is unavailable. Try again later.")
        elif action == "p" and len(parts) == 4 and parts[3].isdigit():
            await q.answer()
            try:
                poet_id = int(parts[3])
                if poet_id not in {pid for pid, _ in await self.ganjoor.poets()}:
                    raise ValueError("Unknown poet")
                await q.message.answer(render_poem(await self.ganjoor.random(poet_id)))
            except (ValueError, httpx.HTTPError):
                await q.message.answer("This poet's work is unavailable. Try again later.")
        elif action == "search":
            self.store.db.execute("UPDATE community_sessions SET kind='poem_search' WHERE id=?", (sid,))
            await q.answer()
            prompt = await q.message.answer("Reply with a word or phrase to search Ganjoor poetry.")
            self.store.db.execute("UPDATE community_sessions SET message_id=? WHERE id=?",
                                  (prompt.message_id, sid))
        return True

    async def search_reply(self, m: Message) -> bool:
        if not m.from_user or not m.reply_to_message or not m.text:
            return False
        row = self.store.db.execute("SELECT id FROM community_sessions WHERE user_id=? AND chat_id=? "
            "AND message_id=? AND kind='poem_search' AND expires>? LIMIT 1",
            (m.from_user.id, m.chat.id, m.reply_to_message.message_id, time.time())).fetchone()
        if row:
            self.store.db.execute("DELETE FROM community_sessions WHERE id=?", (row[0],))
            term = m.text.strip()[:100]
            try:
                poem = await self.ganjoor.search(term)
                if poem:
                    await m.reply(render_poem(poem))
                else:
                    await m.reply("No matching poem found in Ganjoor.")
            except (ValueError, httpx.HTTPError):
                local_poem = self.store.content("poem", query=term)
                await m.reply(self.render_content(local_poem) if local_poem else
                              "Poetry search is unavailable. Try again later.")
            return True
        return False
