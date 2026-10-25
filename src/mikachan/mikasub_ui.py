"""Telegram-native MikaSub browse, converter, and private Owner source controls."""

from __future__ import annotations

import asyncio
import html
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from aiogram.exceptions import TelegramAPIError
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from qrcode.exceptions import DataOverflowError  # type: ignore[import-untyped]

from .access import AccessRegistry
from .mikasub_codec import parse_subscription
from .mikasub_core import MikaSubCore, MikaSubError
from .mikasub_fetch import fetch_subscription, source_url
from .mikasub_viewer import offline_viewer
from .store import Store
from .utilities import qr_png


def buttons(token: str, rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"ms:{token}:{action}")
         for label, action in row] for row in rows])


@dataclass
class Session:
    token: str
    user: int
    chat: int
    expires: float
    owner: bool = False
    settings_token: str = ""
    messages: set[int] = field(default_factory=set)
    version: str = ""
    total: int = 0
    converted: list[dict[str, str]] | None = None
    next_offset: int = 0
    pending: str = ""
    prompt_id: int = 0
    page: str = "home"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MikaSubUI:
    def __init__(self, core: MikaSubCore, store: Store, bot, access: AccessRegistry):
        self.core, self.store, self.bot, self.access = core, store, bot, access
        self.sessions: dict[str, Session] = {}
        self.conversion_gate = asyncio.Semaphore(2)
        self.owner_tasks: set[asyncio.Task[None]] = set()

    def _new(self, user: int, chat: int, *, owner: bool = False,
             settings_token: str = "") -> Session:
        now = time.time()
        self.sessions = {key: item for key, item in self.sessions.items()
                         if item.expires > now and (item.user, item.chat) != (user, chat)}
        if len(self.sessions) >= 16:
            oldest = min(self.sessions, key=lambda key: self.sessions[key].expires)
            self.sessions.pop(oldest)
        token = secrets.token_urlsafe(8)
        session = Session(token, user, chat, now + (900 if owner else 1800),
                          owner=owner, settings_token=settings_token)
        self.sessions[token] = session
        return session

    @staticmethod
    def _bound(session: Session, q: CallbackQuery) -> bool:
        return bool(q.from_user and isinstance(q.message, Message)
                    and session.expires > time.time() and q.from_user.id == session.user
                    and q.message.chat.id == session.chat
                    and q.message.message_id in session.messages)

    async def open_user(self, m: Message) -> None:
        assert m.from_user is not None
        session = self._new(m.from_user.id, m.chat.id)
        sent = await m.reply("🏴‍☠️ <b>MikaSub</b>\nPublic subscription configs, untested and without connection guarantees.\n\nChoose a path.",
            reply_markup=buttons(session.token, [
                [("🏴‍☠️ MikaSub", "collection")],
                [("🔄 Sub Converter", "converter")],
                [("🙏 Thanks", "thanks")]]))
        session.messages.add(sent.message_id)

    async def open_owner(self, q: CallbackQuery, settings_token: str) -> None:
        assert q.from_user and isinstance(q.message, Message)
        if q.message.chat.type != "private" or self.access.role(q.from_user.id) != "OWNER":
            await q.answer("Owner private chat only")
            return
        session = self._new(q.from_user.id, q.message.chat.id, owner=True,
                            settings_token=settings_token)
        session.messages.add(q.message.message_id)
        await q.answer()
        await self._owner_render(q.message, session, "owner")

    async def _edit(self, message: Message, session: Session, text: str,
                    rows: list[list[tuple[str, str]]]) -> None:
        session.messages.add(message.message_id)
        await message.edit_text(text, reply_markup=buttons(session.token, rows))

    async def _send(self, session: Session, text: str,
                    rows: list[list[tuple[str, str]]]) -> Message:
        sent = await self.bot.send_message(session.chat, text,
                                           reply_markup=buttons(session.token, rows))
        session.messages.add(sent.message_id)
        return sent

    async def _items(self, session: Session, offset: int, limit: int) -> list[dict[str, str]]:
        if session.converted is not None:
            return session.converted[offset:offset + limit]
        return await self.core.items(session.version, offset, limit)

    async def _all(self, session: Session) -> list[dict[str, str]]:
        if session.converted is not None:
            return session.converted
        return await self.core.all_items(session.version, session.total)

    async def _batch(self, session: Session, offset: int) -> None:
        entries = await self._items(session, offset, min(5, session.total - offset))
        if not entries:
            await self._send(session, "End of collection.", [[("🙏 Thanks", "thanks")]])
            return
        end = offset + len(entries)
        rows = [[("▣ QR", f"select:{offset}"),
                 ("5 More", f"more:{end}")] if end < session.total else
                [("▣ QR", f"select:{offset}")]]
        rows.append([("🙏 Thanks", "thanks")])
        intro = f"📋 <b>{offset + 1}–{end} of {session.total}</b> · untested\n\n"
        body = intro + "\n\n".join(f"<code>{html.escape(str(item['uri']))}</code>" for item in entries)
        if end >= session.total:
            body += "\n\nEnd of collection."
        if len(body) <= 3900:
            await self._send(session, body, rows)
        else:
            # Complete five-entry TXT is lossless when Telegram's text limit is too small.
            content = ("\n".join(str(item["uri"]) for item in entries) + "\n").encode()
            sent = await self.bot.send_document(session.chat,
                BufferedInputFile(content, f"MikaSub-{offset + 1}-{end}.txt"),
                caption=f"📋 {offset + 1}–{end} of {session.total}. Complete URIs in TXT · untested.",
                reply_markup=buttons(session.token, rows))
            session.messages.add(sent.message_id)
        session.next_offset = end

    async def _qr(self, session: Session, index: int) -> None:
        entries = await self._items(session, index, 1)
        if not entries:
            raise MikaSubError("this configuration is no longer available")
        value = str(entries[0]["uri"])
        try:
            payload = qr_png(value)
        except (ValueError, OverflowError, DataOverflowError) as exc:
            raise MikaSubError("QR capacity exceeded. Use the complete URI or TXT export") from exc
        sent = await self.bot.send_photo(session.chat, BufferedInputFile(payload, "MikaSub-QR.png"),
            caption="▣ Single config QR · untested",
            reply_markup=buttons(session.token, [[("◀ Back", "stash" if session.pending == f"qr:{index}"
                                                   else f"select:{index - index % 5}"),
                                                   ("🙏 Thanks", "thanks")]]))
        session.messages.add(sent.message_id)

    async def _one(self, session: Session) -> None:
        if session.total < 1:
            raise MikaSubError("no published configurations are available")
        index = secrets.randbelow(session.total)
        entry = (await self._items(session, index, 1))[0]
        uri = str(entry["uri"])
        session.pending = f"qr:{index}"
        rows = [[("▣ QR Code", f"qr:{index}")],
                [("◀ Back", "stash"), ("🙏 Thanks", "thanks")]]
        body = ("🎲 <b>One random config</b>\n🏴‍☠️ Endpoint unknown · untested, no promises.\n\n"
                f"<code>{html.escape(uri)}</code>")
        if len(body) <= 3900:
            await self._send(session, body, rows)
        else:
            sent = await self.bot.send_document(session.chat,
                BufferedInputFile((uri + "\n").encode(), "MikaSub-One.txt"),
                caption="🎲 One complete config · endpoint unknown · untested.",
                reply_markup=buttons(session.token, rows))
            session.messages.add(sent.message_id)

    async def _export(self, session: Session, kind: str) -> None:
        items = await self._all(session)
        if len(items) != session.total:
            raise MikaSubError("collection changed before export; open it again")
        converter = session.converted is not None
        title = "Converted Subscription" if converter else "MikaSub"
        if kind == "txt":
            payload = ("\n".join(str(item["uri"]) for item in items) + "\n").encode()
            filename = "Converted-Subscription.txt" if converter else "MikaSub.txt"
            caption = f"📄 {len(items)} complete, untested share URIs."
        else:
            payload = offline_viewer(items, title=title)
            filename = "Converted-Subscription.html" if converter else "MikaSub.html"
            caption = (f"🌐 Offline HTML Viewer (.html) · {len(items)} untested configs. "
                       "Works without Internet after download; YAML button opens HTML.")
        if len(payload) > 20_000_000:
            raise MikaSubError("export exceeds this bot's 20 MB file budget")
        sent = await self.bot.send_document(session.chat, BufferedInputFile(payload, filename),
            caption=caption, reply_markup=buttons(session.token,
                [[("◀ Back", "converted" if converter else "stash"),
                  ("🙏 Thanks", "thanks")]]))
        session.messages.add(sent.message_id)

    async def _user_callback(self, q: CallbackQuery, session: Session, action: str) -> None:
        assert isinstance(q.message, Message)
        await q.answer()
        if action == "thanks":
            self.sessions.pop(session.token, None)
            try:
                await q.message.delete()
            except TelegramAPIError:
                await q.message.edit_reply_markup(reply_markup=None)
            return
        if action == "collection":
            session.messages = {q.message.message_id}
            info = await self.core.metadata()
            session.version = str(info["version"])
            session.total = int(info["total"])
            session.converted = None
            session.next_offset = 0
            body = ("🏴‍☠️ <b>MikaSub · The Unknown Collection</b>\n"
                    f"Available: <b>{session.total:,} configurations</b>\n"
                    f"Published: {html.escape(str(info.get('created_at') or 'unknown'))}\n\n"
                    "These configs are untested. They may or may not work; access from Iran is not guaranteed.")
            await self._edit(q.message, session, body,
                [[("🎲 Just One", "one")], [("📦 The Whole Stash", "stash")],
                 [("◀ Back", "home"), ("🙏 Thanks", "thanks")]])
            return
        if action == "home":
            session.prompt_id = 0
            await self._edit(q.message, session, "🏴‍☠️ <b>MikaSub</b>\nChoose a path. Configs are untested.",
                [[("🏴‍☠️ MikaSub", "collection")], [("🔄 Sub Converter", "converter")],
                 [("🙏 Thanks", "thanks")]])
            return
        if action == "converter":
            session.messages = {q.message.message_id}
            session.converted = None
            session.version = ""
            session.total = 0
            sent = await self._send(session,
                "🔄 <b>Sub Converter</b>\nReply to this message with one public HTTP(S) subscription URL. "
                "The URL is processed for this session only. Cancel if you change your mind.\n\n"
                "In a group, other members may have seen your link before I delete it.",
                [[("◀ Back", "home"), ("🙏 Thanks", "thanks")]])
            session.prompt_id = sent.message_id
            return
        if action == "converted" or action == "stash":
            if action == "converted" and session.converted is None:
                raise MikaSubError("conversion session expired")
            body = (f"📦 <b>{'Converted Subscription' if action == 'converted' else 'The Whole Stash'}</b>\n"
                    f"{session.total:,} complete share URIs · untested.\n\n"
                    "YAML delivers an Offline HTML Viewer (.html), not a Clash file.")
            rows = [[("📋 Here", "here"), ("📄 TXT", "txt")],
                    [("🌐 YAML", "html")]]
            if action == "stash":
                rows.append([("🔗 MikaSub", "link")])
            rows.append([("◀ Back", "home" if action == "converted" else "collection"),
                         ("🙏 Thanks", "thanks")])
            await self._edit(q.message, session, body, rows)
            return
        if action == "link":
            link = self.core.link()
            if not link:
                raise MikaSubError("Owner has not created the MikaSub link yet")
            await self._send(session,
                "🔗 <b>MikaSub subscription</b>\nAnyone holding this link can read the collection until the Owner rotates it.\n\n"
                f"<code>{html.escape(link)}</code>",
                [[("◀ Back", "stash"), ("🙏 Thanks", "thanks")]])
            return
        if action == "one":
            await self._one(session)
            return
        if action == "here":
            session.next_offset = 0
            await self._batch(session, 0)
            return
        if action.startswith("more:"):
            offset = int(action[5:])
            async with session.lock:
                if offset != session.next_offset or offset >= session.total:
                    await self.bot.send_message(session.chat,
                        "This next batch was already opened or the collection ended.")
                    return
                await self._batch(session, offset)
            return
        if action.startswith("select:"):
            start = int(action[7:])
            if start < 0 or start >= session.total:
                raise MikaSubError("QR batch is unavailable")
            count = min(5, session.total - start)
            if count == 1:
                await self._qr(session, start)
                return
            rows = [[(f"{start + index + 1}", f"qr:{start + index}") for index in range(count)],
                    [("◀ Back", "stash" if session.converted is None else "converted"),
                     ("🙏 Thanks", "thanks")]]
            await self._send(session, "▣ Choose one configuration for its QR code.", rows)
            return
        if action.startswith("qr:"):
            index = int(action[3:])
            if not 0 <= index < session.total:
                raise MikaSubError("QR configuration unavailable")
            await self._qr(session, index)
            return
        if action in {"txt", "html"}:
            await self._export(session, action)
            return
        raise MikaSubError("unknown MikaSub action")

    async def callback(self, q: CallbackQuery, data: str) -> bool:
        if not data.startswith("ms:"):
            return False
        parts = data.split(":", 2)
        if len(parts) != 3:
            await q.answer("Menu unavailable")
            return True
        session = self.sessions.get(parts[1])
        if not session or not self._bound(session, q):
            await q.answer("Menu expired or belongs to another user")
            return True
        assert isinstance(q.message, Message)
        if not self.access.can_access(session.user, session.chat, q.message.chat.type):
            await q.answer("Access unavailable")
            return True
        if session.owner and (q.message.chat.type != "private" or
                              self.access.role(session.user) != "OWNER"):
            await q.answer("Owner private chat only")
            return True
        try:
            if session.owner:
                await self._owner_callback(q, session, parts[2])
            else:
                await self._user_callback(q, session, parts[2])
        except (MikaSubError, ValueError, OSError, TelegramAPIError) as exc:
            # Source URLs never appear in a rendered error.
            error = str(exc) if isinstance(exc, MikaSubError) else "MikaSub could not finish this action"
            await self.bot.send_message(session.chat, "⚠️ " + html.escape(error[:180]))
        return True

    async def reply(self, m: Message) -> bool:
        if not m.from_user or not m.text or not m.reply_to_message:
            return False
        for session in list(self.sessions.values()):
            if (session.user != m.from_user.id or session.chat != m.chat.id or
                    session.expires <= time.time() or
                    session.prompt_id != m.reply_to_message.message_id):
                continue
            if not self.access.can_access(session.user, session.chat, m.chat.type):
                return True
            if session.owner:
                if m.chat.type == "private" and self.access.role(session.user) == "OWNER":
                    await self._owner_reply(m, session)
                return True
            if not self.store.rate(session.user, "mikasub_convert", 2):
                await m.reply("Conversion limit reached. Try again in a minute.")
                return True
            try:
                value = source_url(m.text.strip())
            except ValueError:
                await m.reply("Send one valid public HTTP(S) subscription URL as a reply.")
                return True
            session.prompt_id = 0
            if m.chat.type in {"group", "supergroup"}:
                try:
                    await m.delete()
                except TelegramAPIError:
                    pass
            progress = await self.bot.send_message(m.chat.id, "🔄 Reading and converting the subscription…")
            try:
                async with self.conversion_gate:
                    parsed = parse_subscription(await fetch_subscription(value))
                if not parsed.nodes:
                    raise MikaSubError("no losslessly exportable share URIs found")
                session.converted = [{"uri": node.uri, "protocol": node.protocol,
                                      "name": node.name or "Unnamed"} for node in parsed.nodes]
                session.total = len(session.converted)
                session.next_offset = 0
                message = (f"🔄 <b>Conversion ready</b>\nExportable: {session.total:,} · "
                           f"Duplicates: {parsed.duplicates} · Unsupported: {parsed.unsupported} · "
                           f"Malformed: {parsed.errors}\n\n"
                           "Configs are untested. YAML sends an Offline HTML Viewer (.html).")
                await progress.edit_text(message, reply_markup=buttons(session.token,
                    [[("📋 Here", "here"), ("🌐 YAML", "html")],
                     [("📄 TXT", "txt"), ("🙏 Thanks", "thanks")],
                     [("◀ Back", "home")]]))
                session.messages.add(progress.message_id)
            except (ValueError, OSError, asyncio.TimeoutError) as exc:
                status = re.fullmatch(r"subscription fetch returned HTTP (\d{3})", str(exc))
                reason = (f"The source returned HTTP {status[1]}. " if status else
                          "The source could not be read or its format is unsupported. ")
                await progress.edit_text("⚠️ " + reason +
                    "Its URL was not saved or published. Check the source endpoint; "
                    "HTTPS is preferred when the URL contains an access token.")
            return True
        return False

    async def _owner_render(self, message: Message, session: Session, page: str) -> None:
        session.page = page
        back = [("◀ Back", "owner"), ("🙏 Thanks", "thanks")]
        if page == "owner":
            await self._edit(message, session,
                "🏴‍☠️ <b>MikaSub · Owner Control</b>\nSources and publication. Fetch Now reads documents only; no proxy is tested.",
                [[("📂 Sources", "sources:0")], [("🔄 Update Now", "update")],
                 [("🔗 Publication", "publication"), ("📊 Statistics", "statistics")],
                 [("🏷 Naming", "naming")],
                 [("◀ Settings", "settings"), ("🙏 Thanks", "thanks")]])
            return
        if page.startswith("sources:"):
            offset = int(page.split(":", 1)[1])
            sources = await self.core.sources()
            section = sources[offset:offset + 8]
            lines = ["📂 <b>Sources</b>", f"{len(sources)} registered · URLs are kept private."]
            rows = []
            for item in section:
                label = html.escape(str(item["name"])[:50])
                lines.append(f"• {label} · {'On' if item['enabled'] else 'Paused'} · "
                             f"{item['exported_count']} exportable · "
                             f"{html.escape(str(item['last_error'] or 'No error')[:80])}")
                rows.append([(str(item["name"])[:38], f"source:{item['id']}")])
            rows.append([("➕ Add Source", "add")])
            navigation = []
            if offset:
                navigation.append(("◀ Previous", f"sources:{max(0, offset - 8)}"))
            if offset + 8 < len(sources):
                navigation.append(("Next ▶", f"sources:{offset + 8}"))
            if navigation:
                rows.append(navigation)
            rows.append(back)
            await self._edit(message, session, "\n".join(lines), rows)
            return
        if page.startswith("source:"):
            ident = int(page.split(":", 1)[1])
            matches = [value for value in await self.core.sources()
                       if int(value["id"]) == ident]
            source_entry = matches[0] if matches else None
            if source_entry is None:
                raise MikaSubError("source no longer exists")
            body = (f"📂 <b>{html.escape(str(source_entry['name']))}</b> · "
                    f"{'Enabled' if source_entry['enabled'] else 'Disabled'}\n"
                    f"Last fetch: {html.escape(str(source_entry['last_ok'] or 'Never'))}\n"
                    f"Raw: {source_entry['raw_count']} · Parsed: {source_entry['valid_count']} · Exportable: {source_entry['exported_count']}\n"
                    f"Status: {html.escape(str(source_entry['last_error'] or 'No error')[:120])}\n\n"
                    "Fetch Now reads only the subscription document; it never tests listed servers.")
            await self._edit(message, session, body,
                [[("✏️ Rename", f"rename:{ident}"), ("🔗 Replace URL", f"replace:{ident}")],
                 [("⏸ Disable" if source_entry["enabled"] else "▶️ Enable", f"toggle:{ident}")],
                 [("📥 Fetch Now", f"fetch:{ident}"), ("🗑 Remove", f"remove:{ident}")],
                 [("◀ Sources", "sources:0"), ("🙏 Thanks", "thanks")]])
            return
        if page == "publication":
            data = await self.core.publication()
            link = self.core.link()
            body = ("🔗 <b>Publication</b>\n"
                    f"Status: {'Published' if data.get('ready') else 'No published snapshot'}\n"
                    f"Available: {data.get('total', 0)} · Last build: "
                    f"{html.escape(str(data.get('published_at') or 'Never'))}\n\n"
                    + (f"<code>{html.escape(link)}</code>\n\n" if link else "No public link saved in this bot. Rotate to create one.\n\n")
                    + "Anyone holding the URL can read configs until you rotate it.")
            await self._edit(message, session, body,
                [[("🔐 Rotate Link", "rotate")], back])
            return
        if page == "statistics":
            data = await self.core.publication()
            sources = await self.core.sources()
            stats = data.get("stats") or {}
            body = ("📊 <b>MikaSub Statistics</b>\n"
                    f"Sources: {sum(bool(s['enabled']) for s in sources)} active · "
                    f"{sum(not bool(s['enabled']) for s in sources)} disabled\n"
                    f"Raw: {stats.get('raw', 0)} · Parsed: {stats.get('parsed', 0)}\n"
                    f"Exported: {stats.get('exported', 0)} · Duplicates: {stats.get('duplicates', 0)}\n"
                    f"Unsupported: {stats.get('unsupported', 0)} · Malformed: {stats.get('errors', 0)}\n"
                    f"Snapshot: {html.escape(str(data.get('published_at') or 'None'))}\n"
                    f"Build status: {html.escape(self.core.last_status)}\n\n"
                    "No proxy reachability or latency is measured.")
            await self._edit(message, session, body, [back])
            return
        if page == "naming":
            await self._edit(message, session,
                "🏷 <b>Naming</b>\nStable node IDs are kept in D1. Only display names change.\n\n"
                "<code>🏴‍☠️ Maybe It Works 0001</code>\n"
                "<code>🏴‍☠️ Maybe It Works 0002</code>\n\n"
                "IDs above 9999 keep their full number.", [back])

    async def _owner_callback(self, q: CallbackQuery, session: Session, action: str) -> None:
        assert isinstance(q.message, Message)
        await q.answer()
        if action == "thanks":
            self.sessions.pop(session.token, None)
            try:
                await q.message.delete()
            except TelegramAPIError:
                await q.message.edit_reply_markup(reply_markup=None)
            return
        if action == "settings":
            self.sessions.pop(session.token, None)
            await q.message.edit_text("⚙️ Return to Owner Settings below.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="◀ Owner Settings",
                        callback_data=f"z:{session.settings_token}:home")]]))
            return
        if action in {"owner", "publication", "statistics", "naming"} or action.startswith(("sources:", "source:")):
            session.pending = ""
            session.prompt_id = 0
            await self._owner_render(q.message, session, action)
            return
        if action == "update":
            if self.core.build_lock.locked():
                raise MikaSubError("a collection update is already in progress")
            await self._edit(q.message, session,
                "🔄 Updating the published collection. Sources are fetched, never tested as proxies. "
                "The previous snapshot remains available until a complete build is published.",
                [[("📊 Statistics", "statistics"), ("◀ Back", "owner")]])
            task = asyncio.create_task(self._owner_build(session.user, session.chat))
            self.owner_tasks.add(task)
            task.add_done_callback(self.owner_tasks.discard)
            return
        if action == "add" or action.startswith(("rename:", "replace:")):
            session.pending = action
            prompt = ("Reply with: PUBLIC_SUBSCRIPTION_URL | Optional source name"
                      if action == "add" else "Reply with a new source name (1–80 characters)"
                      if action.startswith("rename:") else "Reply with the replacement public subscription URL")
            sent = await self._send(session, prompt + "\nCancel with /cancelmikasub.",
                                    [[("◀ Cancel", "owner")]])
            session.prompt_id = sent.message_id
            return
        if action.startswith("toggle:"):
            ident = int(action[7:])
            source = next((item for item in await self.core.sources() if int(item["id"]) == ident), None)
            if not source:
                raise MikaSubError("source no longer exists")
            await self.core.api("PATCH", f"/api/v1/sources/{ident}",
                                data={"enabled": not bool(source["enabled"])})
            await self._owner_render(q.message, session, f"source:{ident}")
            return
        if action.startswith("fetch:"):
            ident = int(action[6:])
            await self._edit(q.message, session,
                "📥 Fetching the source document. Proxy nodes are not contacted.",
                [[("◀ Sources", "sources:0")]])
            task = asyncio.create_task(self._owner_fetch(session.user, session.chat, ident))
            self.owner_tasks.add(task)
            task.add_done_callback(self.owner_tasks.discard)
            return
        if action.startswith("remove:") or action == "rotate":
            session.pending = action
            label = "Remove this source?" if action.startswith("remove:") else \
                    "Rotate the public subscription URL? The old URL will be revoked."
            await self._edit(q.message, session, label,
                [[("✅ Confirm", "confirm"), ("◀ Cancel", session.page)]])
            return
        if action == "confirm":
            pending = session.pending
            session.pending = ""
            if pending == "rotate":
                await self.core.rotate_link()
                await self._owner_render(q.message, session, "publication")
                return
            if pending.startswith("remove:"):
                await self.core.api("DELETE", f"/api/v1/sources/{int(pending[7:])}")
                await self._owner_render(q.message, session, "sources:0")
                return
        raise MikaSubError("Owner action expired or unavailable")

    async def _owner_build(self, user: int, chat: int) -> None:
        try:
            result = await self.core.rebuild()
            try:
                await self.core.cleanup()
            except (ValueError, OSError, asyncio.TimeoutError):
                pass
            notice = f"✅ MikaSub published {result.total:,} untested configurations. Snapshot {result.version}."
        except (ValueError, OSError, asyncio.TimeoutError):
            notice = "⚠️ MikaSub update failed. The last published collection was retained. Open Statistics for status."
        if self.access.role(user) == "OWNER" and self.access.can_access(user, chat, "private"):
            await self.bot.send_message(chat, notice)

    async def _owner_fetch(self, user: int, chat: int, ident: int) -> None:
        try:
            result = await self.core.fetch_one(ident)
            notice = (f"✅ Source fetched: {len(result.nodes)} exportable; {result.unsupported} unsupported. "
                      "No proxy was tested. Use Update Now to publish.")
        except (ValueError, OSError, asyncio.TimeoutError):
            notice = "⚠️ Source fetch failed. Published configs were not changed."
        if self.access.role(user) == "OWNER" and self.access.can_access(user, chat, "private"):
            await self.bot.send_message(chat, notice)

    async def _owner_reply(self, m: Message, session: Session) -> None:
        assert m.reply_to_message is not None
        value = (m.text or "").strip()
        pending = session.pending
        if value == "/cancelmikasub":
            session.pending = ""
            session.prompt_id = 0
            await m.reply("MikaSub input cancelled.")
            return
        if not pending or session.prompt_id != m.reply_to_message.message_id:
            return
        if pending == "add" or pending.startswith("replace:"):
            try:
                raw_url = value.split("|", 1)[0].strip() if pending == "add" else value
                source_url(raw_url)
            except ValueError:
                await m.reply("Send one valid public HTTP(S) subscription URL.")
                return
            session.prompt_id = 0
            try:
                await m.delete()
            except TelegramAPIError:
                pass
            progress = await self.bot.send_message(session.chat,
                "🔄 Validating the subscription document; no proxy is contacted…")
            try:
                if pending == "add":
                    name = value.split("|", 1)[1].strip() if "|" in value else \
                        (urlsplit(raw_url).hostname or "Source")[:80]
                    await self.core.add_source(name, raw_url)
                    response = "✅ Source added. Use Update Now to publish a new snapshot."
                else:
                    await self.core.replace_source(int(pending[8:]), raw_url)
                    response = "✅ Source URL replaced. Use Update Now to publish."
            except (ValueError, OSError, asyncio.TimeoutError):
                response = "⚠️ Source URL or document could not be validated. Nothing was changed."
            await progress.edit_text(response)
        elif pending.startswith("rename:"):
            if not 1 <= len(value) <= 80:
                await m.reply("Use a source name of 1–80 characters.")
                return
            session.prompt_id = 0
            try:
                await self.core.api("PATCH", f"/api/v1/sources/{int(pending[7:])}",
                                    data={"name": value})
            except (ValueError, OSError, asyncio.TimeoutError):
                await m.reply("⚠️ Source could not be renamed. Try again later.")
            else:
                await m.reply("✅ Source renamed.")
        session.pending = ""

    async def close(self) -> None:
        for task in self.owner_tasks:
            task.cancel()
        if self.owner_tasks:
            await asyncio.gather(*self.owner_tasks, return_exceptions=True)
        self.sessions.clear()
