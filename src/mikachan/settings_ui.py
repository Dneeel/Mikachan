"""Small Telegram-native controls over the persisted access registry."""

import asyncio
import html
import json
import secrets
import sqlite3
import time

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .access import AccessRegistry
from .now_service import NowService


def markup(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows])


class SettingsUI:
    def __init__(self, db: sqlite3.Connection, access: AccessRegistry, now: NowService,
                 bot: Bot | None = None, local_sources=None, mikasub=None, content_ui=None,
                 n8n_gateway=None, audio_ui=None):
        self.db, self.access, self.now, self.bot = db, access, now, bot
        self.local_sources = local_sources
        self.mikasub = mikasub
        self.content_ui = content_ui
        self.n8n_gateway = n8n_gateway
        self.audio_ui = audio_ui
        self.broadcast_tasks: dict[str, asyncio.Task[None]] = {}
        db.executescript("""
        CREATE TABLE IF NOT EXISTS settings_sessions (
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
          message_id INTEGER, page TEXT NOT NULL, pending TEXT NOT NULL DEFAULT '{}',
          expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS settings_captures (
          user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, prompt_id INTEGER NOT NULL,
          token TEXT NOT NULL, kind TEXT NOT NULL, expires REAL NOT NULL,
          PRIMARY KEY(user_id,chat_id));
        CREATE TABLE IF NOT EXISTS message_drafts (
          owner_id INTEGER PRIMARY KEY, body TEXT NOT NULL DEFAULT '',
          audience TEXT NOT NULL DEFAULT 'both', updated REAL NOT NULL);
        """)

    def _session(self, token: str, user: int, chat: int, message: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM settings_sessions WHERE token=? AND user_id=? "
                               "AND chat_id=? AND message_id=? AND expires>?",
                               (token, user, chat, message, time.time())).fetchone()

    async def close(self) -> None:
        tasks = list(self.broadcast_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def show(self, m: Message) -> None:
        if not m.from_user or not self.access.can_access(m.from_user.id, m.chat.id, m.chat.type):
            return
        token = secrets.token_urlsafe(9)
        owner_private = m.chat.type == "private" and self.access.role(m.from_user.id) == "OWNER"
        page = "home" if owner_private else "personal"
        self.db.execute("INSERT INTO settings_sessions(token,user_id,chat_id,message_id,page,expires) "
                        "VALUES (?,?,?,NULL,?,?)",
                        (token, m.from_user.id, m.chat.id, page, time.time() + 900))
        body, buttons = self._render(token, m.from_user.id, m.chat.id, page)
        sent = await m.reply(body, reply_markup=buttons)
        self.db.execute("UPDATE settings_sessions SET message_id=? WHERE token=?",
                        (sent.message_id, token))

    def _render(self, token: str, user: int, chat: int, page: str) -> tuple[str, InlineKeyboardMarkup]:
        def button(label: str, action: str) -> tuple[str, str]:
            return label, f"z:{token}:{action}"
        if page == "home":
            sections = [
                [button("👥 Access Control", "people")],
                [button("🏘 Group Registry", "groups")],
            ]
            if self.local_sources is not None:
                sections.append([button("📂 Local Sources", "locals")])
            if self.mikasub is not None:
                sections.append([button("🏴‍☠️ MikaSub", "mikasub")])
            if self.content_ui is not None:
                sections.append([button("🐈 Cat Approval Pool", "catpool")])
            if self.n8n_gateway is not None:
                sections.append([button("🎙 AI & Audio", "ai_audio")])
            sections += [
                [button("📝 Global Message Studio", "messages")],
                [button("🎭 Personality & Identity", "identity")],
                [button("🛠 System & Operations", "system")],
                [button("👤 My Preferences", "personal")],
                [button("✖ Close", "close")]]
            return ("⚙️ <b>Mikachan · Owner Control</b>\nChoose a section.", markup(sections))
        if page == "personal":
            pref = self.now.preference(user)
            city = "Saved" if pref else "Not set"
            return (f"⚙️ <b>My Preferences</b>\nHome city · {city}\n"
                    "Search filters · set in result views\nCalendar fields · /now settings\n"
                    "Follow preferences · /following", markup([
                    [button("📍 My Location", "location")],
                    [button("◀ Back", "home" if chat == user else "close"),
                     button("✖ Close", "close")]]))
        if page == "catpool" and self.content_ui is not None:
            cats = self.content_ui.approved_cats()
            cat_controls = [[button("👀 Review TheCatAPI candidate", "catreview:thecatapi")],
                            [button("👀 Review CATAAS candidate", "catreview:cataas")]]
            for item in cats[:20]:
                cat_controls.append([button(f"⛔ Ban {item.provider} · {item.identifier[:16]}",
                                            f"catban:{item.provider}:{item.identifier}")])
            cat_controls.append([button("◀ Back", "home")])
            return (f"🐈 <b>Approved Cats</b> · {len(cats)}\n"
                    "Only reviewed IDs can appear in /pishi. Review photos before approval.",
                    markup(cat_controls))
        if page == "ai_audio" and self.n8n_gateway is not None:
            ai_status = "Configured" if self.n8n_gateway.enabled else "Missing"
            tts = "Enabled" if self.audio_ui and self.audio_ui.tts_enabled else "Off"
            stt = "Enabled" if self.audio_ui and self.audio_ui.stt_enabled else "Off"
            return ("🎙 <b>AI & Audio</b>\n"
                    f"n8n connection · {ai_status}\n"
                    "API contract · mika.n8n.v1\n"
                    f"TTS · {tts} · STT · {stt}\n"
                    "Provider entitlement · Unverified\n"
                    "Generation also requires a real eligible catalog and measured free quota.",
                    markup([[button("🩺 Test Safe Ping", "ai_ping")],
                            [button("🎤 Check Model Catalog", "ai_catalog")],
                            [button("🌍 Content Health", "content_health")],
                            [button("◀ Back", "home")]]))
        if page == "content_health" and self.content_ui is not None:
            count = len(self.content_ui.approved_cats())
            banned = int(self.db.execute("SELECT COUNT(*) FROM cat_approval WHERE status='BANNED'")
                         .fetchone()[0])
            return (f"🌍 <b>Content Health</b>\nApproved cat images · {count}\n"
                    f"Banned cat IDs · {banned}\n"
                    "Sources · TheCatAPI, CATAAS, iNaturalist, Cleveland Museum, "
                    "Wikimedia, Wikiquote, Wiktionary\n"
                    "Live source availability is measured only when a request runs.",
                    markup([[button("◀ Back", "ai_audio")]]))
        if page == "messages":
            draft = self.db.execute("SELECT body,audience FROM message_drafts WHERE owner_id=?",
                                    (user,)).fetchone()
            body = str(draft["body"]) if draft else ""
            audience = str(draft["audience"]) if draft else "both"
            recipients = self._recipients(audience)
            return ("📝 <b>Global Message Studio</b>\n"
                    f"Audience · {html.escape(audience.title())} ({len(recipients)} chats)\n"
                    f"Draft · {html.escape(body[:180]) if body else 'Empty'}\n\n"
                    "Compose, preview, then confirm delivery.", markup([
                    [button("✏️ Compose", "studio_compose")],
                    [button("👤 People", "studio_people"),
                     button("🏘 Groups", "studio_groups"),
                     button("🌐 Both", "studio_both")],
                    [button("👁 Preview", "studio_preview"),
                     button("📨 Send", "studio_send")],
                    [button("◀ Back", "home")]]))
        if page == "location":
            pref = self.now.preference(user)
            name = (html.escape(str(pref["city"])) + ", " + html.escape(str(pref["country"]))) \
                if pref and chat == user else ("Saved" if pref else "Not set")
            location_rows = [[button("✏️ Change City", "city")]]
            if pref:
                location_rows.append([button("🗑 Clear City", "clearcity")])
            location_rows.append([button("◀ Back", "personal")])
            return (f"📍 <b>My Location</b>\nHome city · {name}\n"
                    "A city saved here is used by /weather and /now. "
                    "Updates made in a group are visible there.", markup(location_rows))
        if page == "locals" and self.local_sources is not None:
            sources = self.db.execute("SELECT * FROM local_sources ORDER BY name LIMIT 50").fetchall()
            lines = ["📂 <b>Local Sources</b>",
                     "Telegram chats and channels joined by the user account.",
                     "Books: PDF/EPUB. Music and podcasts: playable audio files. "
                     "A connected user account is required."]
            lines += [f"• {html.escape(str(r['name']))} · <code>{r['chat_id']}</code> "
                      f"· {html.escape(str(r['categories']))} · {r['status']}" for r in sources]
            local_controls = [[button("➕ Add source", "local_add")]]
            local_controls += [[button(str(r["name"])[:50], f"local:{r['chat_id']}")]
                         for r in sources]
            local_controls.append([button("⌂ Home", "home")])
            return "\n".join(lines), markup(local_controls)
        if page.startswith("local:") and self.local_sources is not None:
            try:
                source_id = int(page.split(":", 1)[1])
            except ValueError:
                return self._render(token, user, chat, "locals")
            source = self.db.execute("SELECT * FROM local_sources WHERE chat_id=?",
                                     (source_id,)).fetchone()
            if not source:
                return self._render(token, user, chat, "locals")
            return (f"📂 <b>{html.escape(str(source['name']))}</b>\n"
                    f"ID · <code>{source_id}</code>\n"
                    f"Categories · {html.escape(str(source['categories']))}\n"
                    f"Status · {source['status']}", markup([
                    [button("✏️ Edit", "local_edit")],
                    [button("⏸ Suspend" if source["status"] == "ACTIVE" else "▶️ Resume",
                            "local_suspend" if source["status"] == "ACTIVE" else "local_resume")],
                    [button("🗑 Delete", "local_delete")],
                    [button("◀ Sources", "locals")]]))
        if page == "people" or page.startswith("people@"):
            offset = int(page.split("@", 1)[1]) if "@" in page else 0
            offset = max(0, min(offset, 10000))
            rows = self.db.execute("SELECT user_id,internal_name,role,status FROM people "
                                   "ORDER BY user_id LIMIT 20 OFFSET ?", (offset,)).fetchall()
            total = int(self.db.execute("SELECT COUNT(*) FROM people").fetchone()[0])
            lines = ["👥 <b>People Registry</b>", f"Showing {offset + 1}–{offset + len(rows)} of {total}:"]
            lines.extend(f"• <code>{r['user_id']}</code> · {html.escape(str(r['internal_name']))} "
                         f"· {r['role']} · {r['status']}" for r in rows)
            people_controls = [[button("➕ Add Person", "addperson")]]
            people_controls.extend([button(f"👤 {str(r['internal_name'])[:35]}",
                                           f"person:{r['user_id']}")] for r in rows)
            navigation = []
            if offset:
                navigation.append(button("◀ Previous", f"people@{max(0, offset - 20)}"))
            if offset + 20 < total:
                navigation.append(button("Next ▶", f"people@{offset + 20}"))
            if navigation:
                people_controls.append(navigation)
            people_controls.append([button("⌂ Home", "home")])
            return "\n".join(lines), markup(people_controls)
        if page == "groups" or page.startswith("groups@"):
            offset = int(page.split("@", 1)[1]) if "@" in page else 0
            offset = max(0, min(offset, 10000))
            rows = self.db.execute("SELECT chat_id,internal_name,status FROM group_registry "
                                   "ORDER BY chat_id LIMIT 20 OFFSET ?", (offset,)).fetchall()
            total = int(self.db.execute("SELECT COUNT(*) FROM group_registry").fetchone()[0])
            lines = ["🏘 <b>Group Registry</b>",
                     f"Showing {offset + 1}–{offset + len(rows)} of {total}:"]
            lines.extend(f"• <code>{r['chat_id']}</code> · {html.escape(str(r['internal_name']))} "
                         f"· {r['status']}" for r in rows)
            group_controls = [[button("➕ Add Group", "addgroup")]]
            group_controls.extend([button(f"🏘 {str(r['internal_name'])[:35]}",
                                          f"group:{r['chat_id']}")] for r in rows)
            navigation = []
            if offset:
                navigation.append(button("◀ Previous", f"groups@{max(0, offset - 20)}"))
            if offset + 20 < total:
                navigation.append(button("Next ▶", f"groups@{offset + 20}"))
            if navigation:
                group_controls.append(navigation)
            group_controls.append([button("⌂ Home", "home")])
            return "\n".join(lines), markup(group_controls)
        if page.startswith("person:"):
            uid = int(page.split(":")[1])
            row = self.access.person(uid)
            if not row:
                return self._render(token, user, chat, "people")
            body = (f"👤 <b>{html.escape(str(row['internal_name']))}</b>\n"
                    f"ID · <code>{uid}</code>\nRole · {row['role']}\nStatus · {row['status']}")
            grants = [str(grant[0]) for grant in self.db.execute(
                "SELECT chat_id FROM admin_grants WHERE user_id=? ORDER BY chat_id", (uid,))]
            body += (f"\nPrivate chat · {'On' if self.access.private_enabled(uid) else 'Off'}"
                     f"\nAssigned groups · {', '.join(grants) or 'None'}\n"
                     f"Registered · {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(row['created']))}\n"
                     f"Changed · {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(row['updated']))}")
            if row["suspended_until"]:
                body += f"\nSuspended until · {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(row['suspended_until']))}"
            if row["reason"]:
                body += "\nReason · " + html.escape(str(row["reason"])[:200])
            controls: list[list[tuple[str, str]]] = []
            if uid != self.access.primary_owner and row["status"] == "ACTIVE":
                controls.append([button("💬 Toggle Private Chat", "private")])
            if uid != self.access.primary_owner and row["role"] != "OWNER" and row["status"] != "REVOKED":
                controls.extend([[button("✏️ Rename", "rename")],
                                 [button("🔁 USER / ADMIN", "role")],
                                 [button("⏸ Suspend", "suspend")] if row["status"] == "ACTIVE" else
                                 [button("▶️ Resume", "resume")],
                                 [button("⛔ Revoke", "revoke")]])
                if row["status"] == "ACTIVE":
                    controls.append([button("⏱ Timed suspension", "suspendfor")])
                if user == self.access.primary_owner and row["status"] == "ACTIVE":
                    controls.append([button("👑 Add Co-owner · VPS approval", "request_promote")])
            elif uid != self.access.primary_owner and row["role"] == "OWNER" and \
                    user == self.access.primary_owner:
                controls.append([button("👑 Remove Co-owner · VPS approval", "request_revoke")])
            controls.append([button("◀ Back", "people")])
            return body, markup(controls)
        if page.startswith("group:"):
            gid = int(page.split(":")[1])
            row = self.db.execute("SELECT * FROM group_registry WHERE chat_id=?", (gid,)).fetchone()
            if not row:
                return self._render(token, user, chat, "groups")
            assigned = [str(r[0]) for r in self.db.execute(
                "SELECT user_id FROM admin_grants WHERE chat_id=?", (gid,))]
            body = (f"🏘 <b>{html.escape(str(row['internal_name']))}</b>\n"
                    f"ID · <code>{gid}</code>\nStatus · {row['status']}\n"
                    f"Assigned Admin IDs · {', '.join(assigned) or 'None'}")
            controls = [[button("✏️ Rename", "rename")],
                        [button("👮 Assign / Remove Admin", "assign")]]
            if row["status"] != "REVOKED":
                controls.append([button("⏸ Suspend", "suspend")] if row["status"] == "ACTIVE"
                                else [button("▶️ Resume", "resume")])
                controls.append([button("⛔ Revoke", "revoke")])
            controls.append([button("◀ Back", "groups")])
            return body, markup(controls)
        status = {
            "identity": "Coming soon.",
            "system": (f"Registered people · {len(self.access.allowlist())}\n"
                       f"Active groups · {len(self.access.groups())}\n"
                       "Detailed service and backup controls are available in the VPS mikachan panel."),
        }
        return ("⚙️ <b>" + page.title() + "</b>\n" + status.get(page, "Unavailable"),
                markup([[button("◀ Back", "home")]]))

    def _recipients(self, audience: str) -> list[int]:
        people = [int(row[0]) for row in self.db.execute(
            "SELECT p.user_id FROM people p JOIN private_access a ON a.user_id=p.user_id "
            "WHERE p.status='ACTIVE' AND a.enabled=1 AND p.user_id!=?",
            (self.access.primary_owner,))]
        groups = sorted(self.access.groups())
        return people if audience == "people" else groups if audience == "groups" \
            else people + groups

    async def _broadcast(self, token: str, owner: int, chat: int, message: int,
                         body: str, recipients: list[int]) -> None:
        assert self.bot is not None
        sent = failed = 0
        for recipient in recipients:
            if self.access.role(owner) != "OWNER":
                break
            if recipient > 0 and not self.access.can_access(recipient, recipient, "private"):
                continue
            if recipient < 0 and recipient not in self.access.groups():
                continue
            try:
                await self.bot.send_message(recipient, html.escape(body))
                sent += 1
            except (TelegramAPIError, OSError):
                failed += 1
            await asyncio.sleep(0.1)
        try:
            await self.bot.edit_message_text(
                f"📨 <b>Delivery finished</b>\nSent · {sent}\nFailed · {failed}",
                chat_id=chat, message_id=message,
                reply_markup=markup([[("◀ Studio", f"z:{token}:messages")]]))
        finally:
            self.broadcast_tasks.pop(token, None)

    async def callback(self, q: CallbackQuery, data: str) -> bool:
        if not data.startswith("z:"):
            return False
        if not q.from_user or not isinstance(q.message, Message):
            return True
        parts = data.split(":", 2)
        if len(parts) != 3:
            await q.answer()
            return True
        token, action = parts[1:]
        row = self._session(token, q.from_user.id, q.message.chat.id, q.message.message_id)
        if not row or not self.access.can_access(q.from_user.id, q.message.chat.id,
                                                 q.message.chat.type):
            await q.answer("Menu expired or unavailable")
            return True
        page = str(row["page"])
        owner = q.message.chat.type == "private" and self.access.role(q.from_user.id) == "OWNER"
        if not owner and action not in {"personal", "location", "city", "clearcity",
                                       "confirm", "cancel", "close"}:
            await q.answer("Unavailable")
            return True
        if action == "mikasub" and owner and self.mikasub is not None:
            await self.mikasub.open_owner(q, token)
            return True
        if action.startswith("catreview:") and owner and self.content_ui is not None:
            await q.answer()
            try:
                await self.content_ui.review_cat(q.from_user.id, q.message.chat.id,
                                                 action.split(":", 1)[1])
            except ValueError as exc:
                await q.message.answer("⚠️ " + html.escape(str(exc)))
            return True
        if action.startswith("catban:") and owner and self.content_ui is not None:
            parts = action.split(":", 2)
            if len(parts) == 3:
                row = self.db.execute("SELECT item_json FROM cat_approval WHERE provider=? "
                    "AND image_id=?", (parts[1], parts[2])).fetchone()
                if row:
                    from .content_providers import Item
                    self.content_ui.approve_cat(Item(**json.loads(row[0])), False)
            action = "catpool"
        if action == "ai_ping" and owner and self.n8n_gateway is not None:
            await q.answer()
            try:
                result = await self.n8n_gateway.get_capabilities()
                await q.message.answer("✅ n8n responded with contract " +
                                       html.escape(str(result.get("schema_version", "?"))))
            except ValueError as exc:
                await q.message.answer("⚠️ " + html.escape(str(exc)))
            return True
        if action == "ai_catalog" and owner and self.n8n_gateway is not None:
            await q.answer()
            try:
                tts = await self.n8n_gateway.get_models("tts")
                stt = await self.n8n_gateway.get_models("stt")
                await q.message.answer("🎤 <b>Account catalog</b>\n"
                    f"TTS models · {len(tts.get('result', {}).get('items', []))}\n"
                    f"STT models · {len(stt.get('result', {}).get('items', []))}\n"
                    "Availability does not by itself prove free entitlement.")
            except ValueError as exc:
                await q.message.answer("⚠️ " + html.escape(str(exc)))
            return True
        # Keep Identity as a transient notice; the owner must remain on Home.
        if action == "identity":
            await q.answer("Coming soon")
            return True
        # All other actions are encoded as z:TOKEN:ACTION. A Person or Group
        # action contains one additional colon, preserved by split(":", 2).
        if action.startswith("person:") and not (
                action[7:].isdigit() and 0 < int(action[7:]) < 2**63):
            await q.answer("Invalid person")
            return True
        if action.startswith("group:") and not (
                action[6:].startswith("-") and action[7:].isdigit() and
                -2**63 < int(action[6:]) < 0):
            await q.answer("Invalid group")
            return True
        await q.answer()
        if action == "close":
            self.db.execute("DELETE FROM settings_sessions WHERE token=?", (token,))
            await q.message.edit_reply_markup(reply_markup=None)
            return True
        if self.local_sources is not None and owner:
            if action in {"local_add", "local_edit"} and (
                    action == "local_add" or page.startswith("local:")):
                prompt = ("Reply with: NEGATIVE_CHAT_ID | Name | book,music,podcast"
                          if action == "local_add" else
                          "Reply with: Name | book,music,podcast")
                sent = await q.message.answer(prompt)
                self.db.execute("INSERT INTO settings_captures VALUES (?,?,?,?,?,?) "
                                "ON CONFLICT(user_id,chat_id) DO UPDATE SET "
                                "prompt_id=excluded.prompt_id,token=excluded.token,"
                                "kind=excluded.kind,expires=excluded.expires",
                                (q.from_user.id, q.message.chat.id, sent.message_id,
                                 token, action, time.time() + 300))
                return True
            if action in {"local_suspend", "local_resume", "local_delete"} and page.startswith("local:"):
                source_id = int(page.split(":", 1)[1])
                if action == "local_delete":
                    self.db.execute("UPDATE settings_sessions SET pending=? WHERE token=?",
                                    (json.dumps({"kind": "local_delete", "page": page}), token))
                    await q.message.edit_text("Delete this local source? Search results from it expire.",
                        reply_markup=markup([[('✅ Delete', f'z:{token}:confirm'),
                                              ('❌ Cancel', f'z:{token}:cancel')]]))
                    return True
                self.local_sources.set_status(source_id, "SUSPENDED" if
                    action == "local_suspend" else "ACTIVE")
                body, buttons = self._render(token, q.from_user.id, q.message.chat.id, page)
                await q.message.edit_text(body, reply_markup=buttons)
                return True
        if action == "clearcity":
            self.db.execute("DELETE FROM now_preferences WHERE user_id=?", (q.from_user.id,))
            action = "location"
        if action.startswith("studio_") and page == "messages" and owner:
            if action == "studio_compose":
                sent = await q.message.answer("Reply with the text to broadcast (up to 3000 characters).")
                self.db.execute("INSERT INTO settings_captures VALUES (?,?,?,?,?,?) "
                                "ON CONFLICT(user_id,chat_id) DO UPDATE SET "
                                "prompt_id=excluded.prompt_id,token=excluded.token,"
                                "kind=excluded.kind,expires=excluded.expires",
                                (q.from_user.id, q.message.chat.id, sent.message_id,
                                 token, "messagebody", time.time() + 300))
                return True
            if action in {"studio_people", "studio_groups", "studio_both"}:
                audience = action.removeprefix("studio_")
                self.db.execute("INSERT INTO message_drafts VALUES (?,'',?,?) "
                                "ON CONFLICT(owner_id) DO UPDATE SET audience=excluded.audience,"
                                "updated=excluded.updated", (q.from_user.id, audience, time.time()))
                body, buttons = self._render(token, q.from_user.id, q.message.chat.id, page)
                await q.message.edit_text(body, reply_markup=buttons)
                return True
            draft = self.db.execute("SELECT body,audience FROM message_drafts WHERE owner_id=?",
                                    (q.from_user.id,)).fetchone()
            if not draft or not draft["body"]:
                await q.message.edit_text("Compose a message first.", reply_markup=markup([[
                    ("◀ Studio", f"z:{token}:messages")]]))
                return True
            body = str(draft["body"])
            recipients = self._recipients(str(draft["audience"]))
            if action == "studio_preview":
                await q.message.edit_text("👁 <b>Preview</b>\n\n" + html.escape(body),
                    reply_markup=markup([[("◀ Studio", f"z:{token}:messages"),
                                          ("📨 Send", f"z:{token}:studio_send")]]))
                return True
            if action == "studio_send":
                self.db.execute("UPDATE settings_sessions SET pending=? WHERE token=?",
                                (json.dumps({"kind": "broadcast", "body": body,
                                             "audience": str(draft["audience"])}), token))
                await q.message.edit_text(
                    f"Send this message to {len(recipients)} chats? Delivery cannot be recalled.",
                    reply_markup=markup([[("✅ Confirm", f"z:{token}:confirm"),
                                          ("❌ Cancel", f"z:{token}:cancel")]]))
                return True
        if action in {"city", "addperson", "manageperson", "addgroup", "managegroup",
                      "rename", "assign", "suspendfor"}:
            kind = action + (":" + page if action in {"rename", "assign", "suspendfor"} else "")
            prompts = {
                "city": "Reply with the city name. Cancel with /cancelsettings.",
                "addperson": "Reply with: USER_ID INTERNAL_NAME. Cancel with /cancelsettings.",
                "manageperson": "Reply with a registered numeric user ID.",
                "addgroup": "Reply with: NEGATIVE_GROUP_ID INTERNAL_NAME.",
                "managegroup": "Reply with a registered negative group ID.",
                "rename": "Reply with a new internal name (1–80 characters).",
                "assign": "Reply with: ADMIN_USER_ID add  or  ADMIN_USER_ID remove.",
                "suspendfor": "Reply with MINUTES and an optional reason (1–10080 minutes).",
            }
            sent = await q.message.answer(prompts[action])
            self.db.execute("INSERT INTO settings_captures VALUES (?,?,?,?,?,?) "
                            "ON CONFLICT(user_id,chat_id) DO UPDATE SET "
                            "prompt_id=excluded.prompt_id,token=excluded.token,kind=excluded.kind,"
                            "expires=excluded.expires", (q.from_user.id, q.message.chat.id,
                            sent.message_id, token, kind, time.time() + 300))
            return True
        if action in {"suspend", "resume", "revoke", "role", "private"} and page.startswith(("person:", "group:")):
            if page.startswith("person:") and int(page.split(":")[1]) == self.access.primary_owner:
                return True
            pending = {"kind": action, "page": page}
            self.db.execute("UPDATE settings_sessions SET pending=? WHERE token=?",
                            (json.dumps(pending), token))
            await q.message.edit_text(f"Confirm {html.escape(action)} for {html.escape(page)}?",
                reply_markup=markup([[('✅ Confirm', f'z:{token}:confirm'),
                                      ('❌ Cancel', f'z:{token}:cancel')]]))
            return True
        if action in {"request_promote", "request_revoke"} and page.startswith("person:"):
            pending = {"kind": action, "page": page}
            self.db.execute("UPDATE settings_sessions SET pending=? WHERE token=?",
                            (json.dumps(pending), token))
            await q.message.edit_text("Confirm this Co-owner change request? It requires "
                "independent approval in the VPS panel before taking effect.",
                reply_markup=markup([[("✅ Request", f"z:{token}:confirm"),
                                      ("❌ Cancel", f"z:{token}:cancel")]]))
            return True
        notice = ""
        if action == "confirm" and page == "location":
            try:
                pending = json.loads(row["pending"])
                place = pending["city"]
                if (not isinstance(place, list) or len(place) not in {3, 5} or
                        not all(isinstance(value, str) for value in place[:3]) or
                        len(place) == 5 and not all(isinstance(value, (int, float)) for value in place[3:])):
                    raise ValueError("Invalid city preview")
                city_value = ((place[0], place[1], place[2], float(place[3]), float(place[4]))
                              if len(place) == 5 else (place[0], place[1], place[2]))
                self.now._save_city(q.from_user.id, city_value)
                action = "location"
            except (KeyError, TypeError, ValueError):
                await q.message.edit_text("Location preview expired. Open settings again.")
                return True
        elif action == "confirm" and owner:
            try:
                pending = json.loads(row["pending"])
                selected = str(pending.get("page", ""))
                kind = str(pending.get("kind", ""))
                if kind == "broadcast" and page == "messages":
                    if self.bot is None or token in self.broadcast_tasks:
                        raise ValueError("Message studio is unavailable")
                    body = str(pending.get("body", ""))
                    if not 1 <= len(body) <= 3000:
                        raise ValueError("Invalid broadcast draft")
                    draft = self.db.execute("SELECT body,audience FROM message_drafts "
                                            "WHERE owner_id=?", (q.from_user.id,)).fetchone()
                    if not draft or draft["body"] != body or \
                            draft["audience"] != pending.get("audience"):
                        raise ValueError("Broadcast draft changed; preview again")
                    recipients = self._recipients(str(draft["audience"]))
                    if not recipients:
                        raise ValueError("No eligible chats in this audience")
                    if len(recipients) > 200:
                        raise ValueError("Audience exceeds the 200-chat delivery limit")
                    self.db.execute("UPDATE settings_sessions SET pending='{}' WHERE token=?",
                                    (token,))
                    await q.message.edit_text(f"📨 Sending to {len(recipients)} chats…")
                    self.broadcast_tasks[token] = asyncio.create_task(self._broadcast(
                        token, q.from_user.id, q.message.chat.id, q.message.message_id,
                        body, recipients))
                    return True
                if kind == "local_delete" and selected.startswith("local:") and \
                        self.local_sources is not None:
                    self.local_sources.remove(int(selected.split(":", 1)[1]))
                    action = "locals"
                elif selected.startswith("person:"):
                    uid = int(selected.split(":")[1])
                    if kind == "register":
                        self.access.register(q.from_user.id, uid, str(pending["name"]))
                    elif kind == "private":
                        self.access.set_private(q.from_user.id, uid,
                                                not self.access.private_enabled(uid))
                    elif kind == "rename":
                        self.access.update_person(q.from_user.id, uid, name=str(pending["name"]))
                    elif kind == "role":
                        previous = self.access.person(uid)
                        assert previous is not None
                        self.access.update_person(q.from_user.id, uid,
                                                  role="ADMIN" if previous["role"] == "USER" else "USER")
                    elif kind in {"request_promote", "request_revoke"}:
                        approval = self.access.request_owner_change(q.from_user.id, uid,
                            "PROMOTE" if kind == "request_promote" else "REVOKE")
                        notice = ("🔒 Request pending. On the VPS, an authenticated operator "
                                  "must run:\n<code>sudo mikachan owner-approval approve " +
                                  html.escape(approval) + f" {uid}</code>\n\n")
                    elif kind in {"suspend", "resume", "revoke"}:
                        self.access.update_person(q.from_user.id, uid,
                            status={"suspend": "SUSPENDED", "resume": "ACTIVE",
                                    "revoke": "REVOKED"}[kind])
                    elif kind == "suspendfor":
                        self.access.update_person(q.from_user.id, uid, status="SUSPENDED",
                            suspended_until=time.time() + int(pending["minutes"]) * 60,
                            reason=str(pending.get("reason") or ""))
                    else:
                        raise ValueError("Invalid action")
                elif selected.startswith("group:"):
                    gid = int(selected.split(":")[1])
                    if kind == "register":
                        self.access.add_group(q.from_user.id, gid, str(pending["name"]))
                    elif kind == "rename":
                        name = str(pending["name"])
                        if not 1 <= len(name.strip()) <= 80:
                            raise ValueError("Invalid name")
                        self.db.execute("UPDATE group_registry SET internal_name=?,updated=? "
                                        "WHERE chat_id=?", (name.strip(), time.time(), gid))
                        self.access.audit(q.from_user.id, "rename_group", chat=gid)
                    elif kind == "assign":
                        self.access.assign(q.from_user.id, int(pending["user"]), gid,
                                           bool(pending["grant"]))
                    elif kind in {"suspend", "resume", "revoke"}:
                        self.access.set_group_status(q.from_user.id, gid,
                            {"suspend": "SUSPENDED", "resume": "ACTIVE",
                             "revoke": "REVOKED"}[kind])
                    else:
                        raise ValueError("Invalid action")
                else:
                    raise ValueError("Invalid action")
                if action != "locals":
                    action = selected
            except (ValueError, KeyError, TypeError) as exc:
                await q.message.edit_text(html.escape(str(exc)[:200]))
                return True
        elif action == "cancel":
            action = page
        if action == "home" and not owner:
            action = "personal"
        self.db.execute("UPDATE settings_sessions SET page=?,pending='{}' WHERE token=?",
                        (action, token))
        body, buttons = self._render(token, q.from_user.id, q.message.chat.id, action)
        await q.message.edit_text(notice + body, reply_markup=buttons)
        return True

    async def reply(self, m: Message) -> bool:
        if not m.from_user or not m.text:
            return False
        capture = self.db.execute("SELECT * FROM settings_captures WHERE user_id=? AND chat_id=? "
                    "AND expires>?", (m.from_user.id, m.chat.id, time.time())).fetchone()
        if not capture:
            return False
        if m.text.strip() == "/cancelsettings":
            self.db.execute("DELETE FROM settings_captures WHERE user_id=? AND chat_id=?",
                            (m.from_user.id, m.chat.id))
            await m.reply("Settings input cancelled.")
            return True
        if not m.reply_to_message or m.reply_to_message.message_id != capture["prompt_id"]:
            return False
        token = str(capture["token"])
        menu = self.db.execute("SELECT * FROM settings_sessions WHERE token=? AND user_id=? "
                               "AND chat_id=? AND expires>?", (token, m.from_user.id,
                               m.chat.id, time.time())).fetchone()
        if not menu or not self.access.can_access(m.from_user.id, m.chat.id, m.chat.type):
            return True
        kind = str(capture["kind"])
        page = str(menu["page"])
        value = m.text.strip()
        try:
            if kind == "city":
                place = await self.now.city(value)
                sent = await m.reply("Preview · " + html.escape(", ".join(place[:3])) +
                    "\nSave as your home city?", reply_markup=markup([[
                        ("✅ Save", f"z:{token}:confirm"),
                        ("❌ Cancel", f"z:{token}:cancel")]]))
                self.db.execute("UPDATE settings_sessions SET page='location',pending=?,message_id=? "
                                "WHERE token=?", (json.dumps({"city": place}), sent.message_id, token))
                self.db.execute("DELETE FROM settings_captures WHERE user_id=? AND chat_id=?",
                                (m.from_user.id, m.chat.id))
                return True
            if m.chat.type != "private" or self.access.role(m.from_user.id) != "OWNER":
                return True
            pending: dict[str, object]
            if kind in {"local_add", "local_edit"} and self.local_sources is not None:
                fields = [part.strip() for part in value.split("|")]
                if kind == "local_add" and len(fields) == 3:
                    source_id = int(fields[0])
                    name, categories = fields[1:]
                elif kind == "local_edit" and len(fields) == 2 and page.startswith("local:"):
                    source_id = int(page.split(":", 1)[1])
                    name, categories = fields
                else:
                    raise ValueError("Use CHAT_ID | Name | categories, or Name | categories")
                await self.local_sources.upsert(source_id, name, categories)
                target = f"local:{source_id}"
                body, buttons = self._render(token, m.from_user.id, m.chat.id, target)
                sent = await m.reply(body, reply_markup=buttons)
                self.db.execute("UPDATE settings_sessions SET page=?,message_id=?,pending='{}' "
                                "WHERE token=?", (target, sent.message_id, token))
                self.db.execute("DELETE FROM settings_captures WHERE user_id=? AND chat_id=?",
                                (m.from_user.id, m.chat.id))
                return True
            if kind == "messagebody":
                if not 1 <= len(value) <= 3000:
                    raise ValueError("Message must contain 1–3000 characters")
                self.db.execute("INSERT INTO message_drafts VALUES (?,?,?,?) "
                                "ON CONFLICT(owner_id) DO UPDATE SET body=excluded.body,"
                                "updated=excluded.updated", (m.from_user.id, value, "both",
                                                        time.time()))
                body, buttons = self._render(token, m.from_user.id, m.chat.id, "messages")
                sent = await m.reply(body, reply_markup=buttons)
                self.db.execute("UPDATE settings_sessions SET page='messages',message_id=? "
                                "WHERE token=?", (sent.message_id, token))
                self.db.execute("DELETE FROM settings_captures WHERE user_id=? AND chat_id=?",
                                (m.from_user.id, m.chat.id))
                return True
            if kind in {"addperson", "addgroup"}:
                raw_id, sep, name = value.partition(" ")
                if not sep or not name.strip() or not raw_id.lstrip("-").isdigit():
                    raise ValueError("Enter a numeric ID followed by an internal name")
                number = int(raw_id)
                if kind == "addperson" and not 0 < number < 2**63 or \
                        kind == "addgroup" and not -2**63 < number < 0:
                    raise ValueError("Invalid numeric ID")
                pending = {"kind": "register", "page": f"{'person' if kind == 'addperson' else 'group'}:{number}",
                           "name": name.strip()}
            elif kind in {"manageperson", "managegroup"}:
                number = int(value)
                selected = f"{'person' if kind == 'manageperson' else 'group'}:{number}"
                if kind == "manageperson" and self.access.person(number) is None or \
                        kind == "managegroup" and not self.db.execute(
                            "SELECT 1 FROM group_registry WHERE chat_id=?", (number,)).fetchone():
                    raise ValueError("ID is not registered")
                body, buttons = self._render(token, m.from_user.id, m.chat.id, selected)
                sent = await m.reply(body, reply_markup=buttons)
                self.db.execute("UPDATE settings_sessions SET page=?,message_id=? WHERE token=?",
                                (selected, sent.message_id, token))
                self.db.execute("DELETE FROM settings_captures WHERE user_id=? AND chat_id=?",
                                (m.from_user.id, m.chat.id))
                return True
            elif kind.startswith("rename:"):
                if not 1 <= len(value) <= 80:
                    raise ValueError("Name must be 1–80 characters")
                pending = {"kind": "rename", "page": page, "name": value}
            elif kind.startswith("assign:"):
                uid, operation = value.split()
                if operation not in {"add", "remove"}:
                    raise ValueError("Use ADMIN_USER_ID add or remove")
                pending = {"kind": "assign", "page": page,
                           "user": int(uid), "grant": operation == "add"}
            elif kind.startswith("suspendfor:"):
                raw_minutes, _, reason = value.partition(" ")
                minutes = int(raw_minutes)
                if not 1 <= minutes <= 10080:
                    raise ValueError("Duration must be 1–10080 minutes")
                pending = {"kind": "suspendfor", "page": page,
                           "minutes": minutes, "reason": reason[:200]}
            else:
                raise ValueError("Invalid input")
            sent = await m.reply("Preview · " + html.escape(str(pending)) +
                "\nConfirm this change?", reply_markup=markup([[
                    ("✅ Confirm", f"z:{token}:confirm"),
                    ("❌ Cancel", f"z:{token}:cancel")]]))
            self.db.execute("UPDATE settings_sessions SET pending=?,message_id=? WHERE token=?",
                            (json.dumps(pending), sent.message_id, token))
            self.db.execute("DELETE FROM settings_captures WHERE user_id=? AND chat_id=?",
                            (m.from_user.id, m.chat.id))
        except (ValueError, TypeError, httpx.HTTPError) as exc:
            await m.reply(html.escape(str(exc)[:200]) if not isinstance(exc, httpx.HTTPError)
                          else "Location service is unavailable. Try again later.")
        return True
