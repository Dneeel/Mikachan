"""Group-scoped, admin-managed keyword notes and exact Telegram rules messages.

A saved Telegram message is copied without altering its media/caption when available;
a durable file_id/text snapshot permits best-effort delivery after source removal.
"""

from __future__ import annotations

import html
import json
import re
import secrets
import sqlite3
import time
import unicodedata
from typing import Any

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity

RULES = "__rules__"
WORD = re.compile(r"^[^\W_][\w\u200c-]{0,63}$", re.UNICODE)
MAX_NOTE_TEXT = 4000


def canonical(value: str) -> str:
    return unicodedata.normalize("NFKC", value.replace("ي", "ی").replace("ك", "ک")).strip().casefold()


def note_name(value: str) -> str | None:
    cleaned = canonical(value.removeprefix("@"))
    if not WORD.fullmatch(cleaned) or len(cleaned.encode("utf-8")) > 128:
        return None
    if cleaned in {"قوانین", "notes", "rules"}:
        return None
    return cleaned


def serialize_entities(entities: Any) -> str:
    return json.dumps([e.model_dump(mode="json", exclude_none=True) for e in (entities or [])],
                      ensure_ascii=False)


def restored_entities(source: str | None) -> list[MessageEntity] | None:
    items = json.loads(source or "[]")
    return [MessageEntity.model_validate(item) for item in items] or None


def snapshot(message: Message) -> dict[str, Any] | None:
    """Keep source as-is; never replace caption with note key or change its entities."""
    if getattr(message, "has_protected_content", False):
        return None
    text = getattr(message, "text", None) or ""
    caption = getattr(message, "caption", None) or ""
    media = ""
    media_id = ""
    photos = message.photo
    if photos:
        media, media_id = "photo", photos[-1].file_id
    else:
        for kind in ("animation", "video", "document", "audio", "voice", "sticker",
                     "video_note"):
            item = getattr(message, kind, None)
            if item is not None:
                media, media_id = kind, item.file_id
                break
    if not text and not media and not getattr(message, "poll", None) and not getattr(message, "contact", None):
        return None
    return {
        "source_chat": message.chat.id, "source_message": message.message_id,
        "kind": media or ("text" if text else "copy_only"),
        "body": text, "body_entities": serialize_entities(getattr(message, "entities", None)),
        "media_file_id": media_id, "caption": caption,
        "caption_entities": serialize_entities(getattr(message, "caption_entities", None)),
    }


class GroupContentStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.executescript("""
CREATE TABLE IF NOT EXISTS group_saved_messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 chat_id INTEGER NOT NULL,
 name TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 source_chat INTEGER, source_message INTEGER,
 kind TEXT NOT NULL DEFAULT 'text', body TEXT NOT NULL DEFAULT '',
 body_entities TEXT NOT NULL DEFAULT '[]',
 media_file_id TEXT NOT NULL DEFAULT '', caption TEXT NOT NULL DEFAULT '',
 caption_entities TEXT NOT NULL DEFAULT '[]',
 created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(chat_id,name));
CREATE TABLE IF NOT EXISTS group_content_pending (
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 mode TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', source_message INTEGER,
 prompt_message INTEGER, payload TEXT NOT NULL DEFAULT '', expires REAL NOT NULL,
 PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS group_content_migration (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS group_content_menus (
 token TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 message_id INTEGER, selected_id INTEGER, page INTEGER NOT NULL DEFAULT 0,
 confirm TEXT NOT NULL DEFAULT '', expires REAL NOT NULL);
""")
        # One-time migration prevents a deleted legacy note from reappearing on restart.
        if db.execute("SELECT 1 FROM group_content_migration WHERE name='legacy-v1'").fetchone():
            return
        now = time.time()
        if self._exists("group_notes"):
            db.execute("""INSERT OR IGNORE INTO group_saved_messages
                (chat_id,name,kind,body,created,updated)
                SELECT chat_id,name,'text',body,?,? FROM group_notes
                WHERE trim(body) <> ''""", (now, now))
        if self._exists("group_settings"):
            db.execute("""INSERT OR IGNORE INTO group_saved_messages
                (chat_id,name,kind,body,created,updated)
                SELECT chat_id,?,'text',value,?,? FROM group_settings
                WHERE name='rules' AND trim(value) <> ''""", (RULES, now, now))
        db.execute("INSERT INTO group_content_migration VALUES ('legacy-v1')")

    def _exists(self, table: str) -> bool:
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                               (table,)).fetchone() is not None

    def get(self, chat: int, name: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM group_saved_messages WHERE chat_id=? AND name=?",
                               (chat, name)).fetchone()

    def by_id(self, chat: int, id_: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM group_saved_messages WHERE chat_id=? AND id=?",
                               (chat, id_)).fetchone()

    def list(self, chat: int, offset: int, limit: int = 8) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM group_saved_messages WHERE chat_id=? AND name!=? "
                               "ORDER BY name LIMIT ? OFFSET ?", (chat, RULES, limit, offset)).fetchall()

    def count(self, chat: int) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM group_saved_messages WHERE chat_id=? "
                                   "AND name!=?", (chat, RULES)).fetchone()[0])

    def save(self, chat: int, name: str, values: dict[str, Any]) -> None:
        now = time.time()
        self.db.execute("""INSERT INTO group_saved_messages
          (chat_id,name,active,source_chat,source_message,kind,body,body_entities,
           media_file_id,caption,caption_entities,created,updated)
          VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(chat_id,name) DO UPDATE SET
          active=1,source_chat=excluded.source_chat,source_message=excluded.source_message,
          kind=excluded.kind,body=excluded.body,body_entities=excluded.body_entities,
          media_file_id=excluded.media_file_id,caption=excluded.caption,
          caption_entities=excluded.caption_entities,updated=excluded.updated""",
          (chat, name, values.get("source_chat"), values.get("source_message"),
           values.get("kind", "text"), values.get("body", ""), values.get("body_entities", "[]"),
           values.get("media_file_id", ""), values.get("caption", ""),
           values.get("caption_entities", "[]"), now, now))

    def set_active(self, chat: int, id_: int, active: bool) -> None:
        self.db.execute("UPDATE group_saved_messages SET active=?,updated=? "
                        "WHERE chat_id=? AND id=? AND name!=?",
                        (int(active), time.time(), chat, id_, RULES))

    def delete(self, chat: int, id_: int) -> None:
        self.db.execute("DELETE FROM group_saved_messages WHERE chat_id=? AND id=? AND name!=?",
                        (chat, id_, RULES))

    def del_rules(self, chat: int) -> None:
        self.db.execute("DELETE FROM group_saved_messages WHERE chat_id=? AND name=?", (chat, RULES))
        # Prevent restoration of old rules from the legacy table on restart.
        if self._exists("group_settings"):
            self.db.execute("DELETE FROM group_settings WHERE chat_id=? AND name='rules'", (chat,))

    def pending(self, chat: int, user: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM group_content_pending WHERE chat_id=? AND user_id=? "
                               "AND expires>?", (chat, user, time.time())).fetchone()

    def set_pending(self, chat: int, user: int, mode: str, name: str = "",
                    source_message: int | None = None, prompt_message: int | None = None,
                    payload: dict[str, Any] | None = None) -> None:
        self.db.execute("""INSERT INTO group_content_pending VALUES (?,?,?,?,?,?,?,?)
          ON CONFLICT(chat_id,user_id) DO UPDATE SET mode=excluded.mode,name=excluded.name,
          source_message=excluded.source_message,prompt_message=excluded.prompt_message,
          payload=excluded.payload,expires=excluded.expires""",
          (chat, user, mode, name, source_message, prompt_message,
           json.dumps(payload or {}, ensure_ascii=False), time.time() + 600))

    def clear_pending(self, chat: int, user: int) -> None:
        self.db.execute("DELETE FROM group_content_pending WHERE chat_id=? AND user_id=?",
                        (chat, user))


async def send_saved(bot: Any, row: sqlite3.Row, target_chat: int, reply_to: int | None = None) -> bool:
    """Prefer Telegram's exact copy. Saved file IDs preserve media if source is deleted."""
    if row["source_chat"] and row["source_message"]:
        try:
            await bot.copy_message(target_chat, row["source_chat"], row["source_message"],
                                   reply_to_message_id=reply_to)
            return True
        except Exception:
            pass
    kind = row["kind"]
    opts = {"reply_to_message_id": reply_to}
    try:
        if kind == "text" and row["body"]:
            await bot.send_message(target_chat, row["body"],
                entities=restored_entities(row["body_entities"]), parse_mode=None, **opts)
            return True
        media_id = row["media_file_id"]
        if not media_id:
            return False
        method = getattr(bot, "send_" + kind, None)
        if not method:
            return False
        if kind in {"sticker", "video_note"}:
            await method(target_chat, media_id, **opts)
        else:
            await method(target_chat, media_id, caption=row["caption"] or None,
                         caption_entities=restored_entities(row["caption_entities"]),
                         parse_mode=None, **opts)
        return True
    except Exception:
        return False


class GroupContent:
    def __init__(self, bot: Any, store: GroupContentStore, community: Any):
        self.bot, self.store, self.community = bot, store, community

    async def managed(self, chat: int, user: int) -> bool:
        return await self.community.admin(chat, user)

    async def command(self, m: Message, cmd: str, arg: str) -> bool:
        if cmd not in {"/notes", "/setnote", "/rules", "/setrules", "/delrules"}:
            return False
        if m.chat.type not in {"group", "supergroup"} or not m.from_user:
            return True
        chat, user = m.chat.id, m.from_user.id
        if cmd == "/rules":
            await self.deliver(m, RULES)
            return True
        if not await self.managed(chat, user):
            return True
        if cmd == "/notes":
            await self.menu(m)
        elif cmd == "/setnote":
            match = re.fullmatch(r"\s*@([^\s]+)(?:\s+([\s\S]+))?\s*", arg)
            name = note_name(match.group(1)) if match else None
            if name:
                if m.reply_to_message:
                    await self.save_source(m, name, m.reply_to_message)
                elif match and match.group(2) and len(match.group(2)) <= MAX_NOTE_TEXT:
                    await self.save_text(m, name, match.group(2))
                else:
                    await m.reply("Use /setnote @word TEXT or reply to a message with /setnote @word.")
            elif not arg and m.reply_to_message:
                payload = snapshot(m.reply_to_message)
                if payload is None:
                    await m.reply("This message cannot be saved (unsupported or protected media).")
                    return True
                prompt = await m.reply("Reply to this prompt with @word to name the replied message.")
                self.store.set_pending(chat, user, "name_source", source_message=m.reply_to_message.message_id,
                                       prompt_message=prompt.message_id, payload=payload)
            else:
                await m.reply("Use /setnote @word TEXT or reply with /setnote @word.")
        elif cmd == "/setrules":
            if not m.reply_to_message:
                await m.reply("Reply to the exact rules message with /setrules.")
            else:
                await self.save_source(m, RULES, m.reply_to_message)
        elif cmd == "/delrules":
            if not self.store.get(chat, RULES):
                return True
            token = secrets.token_urlsafe(9)
            markup = self._buttons([[('Yes, delete', f'gc:{token}:rulesyes'),
                                     ('No', f'gc:{token}:rulesno')]])
            prompt = await m.reply("⚠️ Delete group rules?", reply_markup=markup)
            self._new_menu(token, chat, user, prompt.message_id, confirm="rules")
        return True

    async def save_text(self, m: Message, name: str, text: str) -> None:
        if m.from_user is None:
            return
        self.store.save(m.chat.id, name, {"kind": "text", "body": text})
        self.community.store.audit(m.chat.id, m.from_user.id, "note_save", None, name)
        await m.reply(f"✅ Note @{html.escape(name)} saved.")

    async def save_source(self, m: Message, name: str, source: Message) -> None:
        if m.from_user is None:
            return
        item = snapshot(source)
        if item is None:
            await m.reply("This message cannot be saved (unsupported or protected media).")
            return
        self.store.save(m.chat.id, name, item)
        self.community.store.audit(m.chat.id, m.from_user.id,
                                   "rules_save" if name == RULES else "note_save", None, name)
        await m.reply("✅ Rules saved." if name == RULES else f"✅ Note @{html.escape(name)} saved.")

    async def deliver(self, m: Message, name: str) -> bool:
        row = self.store.get(m.chat.id, name)
        if not row or not row["active"]:
            return False
        ok = await send_saved(self.bot, row, m.chat.id, m.message_id)
        if not ok:
            # A broken original message/file ID should never be misreported as delivered.
            await m.reply("Saved content is temporarily unavailable. Ask an admin to update it.")
        return ok

    async def keyword(self, m: Message, text: str) -> bool:
        if m.chat.type not in {"group", "supergroup"} or text.startswith("/"):
            return False
        word = canonical(text)
        if word == "قوانین":
            await self.deliver(m, RULES)
            return True
        name = note_name(word)
        if not name:
            return False
        row = self.store.get(m.chat.id, name)
        if not row or not row["active"]:
            return False
        await self.deliver(m, name)
        return True

    @staticmethod
    def _buttons(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=code) for label, code in row]
            for row in rows])

    def _new_menu(self, token: str, chat: int, user: int, message_id: int,
                  confirm: str = "") -> None:
        self.store.db.execute("INSERT INTO group_content_menus "
                              "(token,chat_id,user_id,message_id,expires,confirm) VALUES (?,?,?,?,?,?)",
                              (token, chat, user, message_id, time.time() + 600, confirm))

    def view(self, token: str, chat: int, selected: int | None = None,
             page: int = 0, confirm: str = "") -> tuple[str, InlineKeyboardMarkup]:
        count = self.store.count(chat)
        page = max(0, min(page, max(0, (count - 1) // 8)))
        rows: list[list[tuple[str, str]]] = []
        if selected is not None:
            entry = self.store.by_id(chat, selected)
            if entry and entry["name"] != RULES:
                rows.append([("Preview", f"gc:{token}:preview")])
                rows.append([("Edit", f"gc:{token}:edit")])
                rows.append([("Disable" if entry["active"] else "Enable", f"gc:{token}:toggle")])
                rows.append([("Delete", f"gc:{token}:delete")])
                if confirm == "note":
                    rows.append([("Yes, delete", f"gc:{token}:yes"),
                                 ("Cancel", f"gc:{token}:no")])
                rows.append([("◀ Back", f"gc:{token}:back")])
                return f"📝 <b>@{html.escape(entry['name'])}</b> · {'Enabled' if entry['active'] else 'Disabled'}", self._buttons(rows)
        for entry in self.store.list(chat, page * 8):
            label = ("✅" if entry["active"] else "⏸") + " @" + str(entry["name"])
            rows.append([(label[:42], f"gc:{token}:open_{entry['id']}")])
        pages = []
        if page:
            pages.append(("◀", f"gc:{token}:page_{page - 1}"))
        if (page + 1) * 8 < count:
            pages.append(("▶", f"gc:{token}:page_{page + 1}"))
        if pages:
            rows.append(pages)
        rows.append([("＋ Add note", f"gc:{token}:add")])
        rows.append([("✖ Close", f"gc:{token}:close")])
        return f"🗂 <b>Group Notes</b> · {count} saved", self._buttons(rows)

    async def menu(self, m: Message) -> None:
        if m.from_user is None:
            return
        token = secrets.token_urlsafe(9)
        body, markup = self.view(token, m.chat.id)
        prompt = await m.reply(body, reply_markup=markup)
        self._new_menu(token, m.chat.id, m.from_user.id, prompt.message_id)

    async def callback(self, q: CallbackQuery, data: str) -> bool:
        if not data.startswith("gc:"):
            return False
        if not q.from_user or not isinstance(q.message, Message):
            await q.answer()
            return True
        parts = data.split(":", 2)
        if len(parts) != 3:
            await q.answer()
            return True
        token, action = parts[1:]
        row = self.store.db.execute("SELECT * FROM group_content_menus WHERE token=? AND "
             "chat_id=? AND user_id=? AND message_id=? AND expires>?",
             (token, q.message.chat.id, q.from_user.id, q.message.message_id, time.time())).fetchone()
        if not row or not await self.managed(q.message.chat.id, q.from_user.id):
            await q.answer("Menu expired or access denied")
            return True
        chat, user = q.message.chat.id, q.from_user.id
        selected, page, confirm = row["selected_id"], row["page"], row["confirm"]
        if action == "close":
            self.store.db.execute("DELETE FROM group_content_menus WHERE token=?", (token,))
            await q.answer()
            await q.message.edit_reply_markup(reply_markup=None)
            return True
        if confirm == "rules":
            if action == "rulesyes":
                self.store.del_rules(chat)
                self.community.store.audit(chat, user, "rules_delete", None, "confirmed")
                await q.answer("Rules deleted")
            else:
                await q.answer("Cancelled")
            self.store.db.execute("DELETE FROM group_content_menus WHERE token=?", (token,))
            await q.message.edit_text("Rules deleted." if action == "rulesyes" else "Deletion cancelled.")
            return True
        if action.startswith("open_") or action.startswith("page_"):
            value = action.split("_", 1)[1]
            if not value.isdecimal():
                await q.answer()
                return True
            if action.startswith("open_"):
                selected = int(value)
                if not self.store.by_id(chat, selected):
                    await q.answer("Note unavailable")
                    return True
            else:
                page, selected = int(value), None
            confirm = ""
        elif action == "back":
            selected, confirm = None, ""
        elif action == "preview" and selected:
            await q.answer()
            item = self.store.by_id(chat, selected)
            if item and not await send_saved(self.bot, item, chat):
                await q.message.answer("Saved content unavailable; edit this note.")
            return True
        elif action == "toggle" and selected:
            item = self.store.by_id(chat, selected)
            if item:
                self.store.set_active(chat, selected, not bool(item["active"]))
                self.community.store.audit(chat, user, "note_toggle", None, str(item["name"]))
        elif action == "delete" and selected:
            confirm = "note"
        elif action == "no":
            confirm = ""
        elif action == "yes" and selected and confirm == "note":
            item = self.store.by_id(chat, selected)
            self.store.delete(chat, selected)
            self.community.store.audit(chat, user, "note_delete", None,
                                       str(item["name"]) if item else "deleted")
            selected, confirm = None, ""
        elif action in {"add", "edit"}:
            item = self.store.by_id(chat, selected) if selected else None
            if action == "edit" and not item:
                await q.answer("Note unavailable")
                return True
            instruction = ("Reply to this prompt with @word TEXT. For original media, "
                           "reply to the media with /setnote @word."
                           if action == "add" else
                           f"Reply to this prompt with the new TEXT, PHOTO, VIDEO or GIF for @"
                           f"{item['name'] if item else ''}.")
            prompt = await q.message.answer(instruction)
            self.store.set_pending(chat, user, "add" if action == "add" else "edit",
                                   str(item["name"]) if item else "", prompt_message=prompt.message_id)
            await q.answer("Ready")
            return True
        else:
            await q.answer()
            return True
        self.store.db.execute("UPDATE group_content_menus SET selected_id=?,page=?,confirm=? WHERE token=?",
                              (selected, page, confirm, token))
        body, markup = self.view(token, chat, selected, page, confirm)
        await q.answer()
        await q.message.edit_text(body, reply_markup=markup)
        return True

    async def pending_message(self, m: Message) -> bool:
        """Capture ONLY replies to own short-lived admin prompt, never ambient group chat."""
        if m.chat.type not in {"group", "supergroup"} or not m.from_user:
            return False
        pending = self.store.pending(m.chat.id, m.from_user.id)
        if not pending or not m.reply_to_message or \
                m.reply_to_message.message_id != pending["prompt_message"]:
            return False
        if not await self.managed(m.chat.id, m.from_user.id):
            self.store.clear_pending(m.chat.id, m.from_user.id)
            return True
        mode = pending["mode"]
        if mode == "name_source":
            name = note_name((m.text or "").strip())
            if not name:
                await m.reply("Reply with a single @word.")
                return True
            # The original was snapshotted when /setnote was received. Telegram's
            # Bot API has no getMessage(chat_id, message_id) method.
            values = json.loads(pending["payload"])
            if not values:
                await m.reply("Original message snapshot has expired; retry /setnote.")
                return True
            self.store.save(m.chat.id, name, values)
            self.store.clear_pending(m.chat.id, m.from_user.id)
            await m.reply(f"✅ Note @{html.escape(name)} saved.")
            return True
        if mode == "add":
            match = re.fullmatch(r"\s*@([^\s]+)\s+([\s\S]+)", m.text or "")
            name = note_name(match.group(1)) if match else None
            if not match or not name or len(match.group(2)) > MAX_NOTE_TEXT:
                await m.reply("Reply with @word TEXT or use /setnote @word on original media.")
                return True
            await self.save_text(m, name, match.group(2))
        elif mode == "edit":
            name = pending["name"]
            if not self.store.get(m.chat.id, name):
                self.store.clear_pending(m.chat.id, m.from_user.id)
                return True
            if m.text is not None:
                if not m.text or len(m.text) > MAX_NOTE_TEXT:
                    await m.reply("Text must be 1–4000 characters.")
                    return True
                await self.save_text(m, name, m.text)
            else:
                await self.save_source(m, name, m)
        self.store.clear_pending(m.chat.id, m.from_user.id)
        return True
