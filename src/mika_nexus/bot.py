"""Native Telegram interface. All actions recheck actor, chat and session ownership."""

import asyncio
import html
import json
import time

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InaccessibleMessage,
    Message,
)

from .config import Settings
from .jobs import Worker
from .search import KINDS, SOURCES, Searcher
from .matching import match_score
from .music_ui import listening_links, verified_lyrics
from .security import allowed, clean_url, links, source
from .store import Store


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


async def acknowledge(q: CallbackQuery, message: str | None = None) -> None:
    try:
        await q.answer(message)
    except TelegramBadRequest as exc:
        if (
            "query is too old" not in str(exc).lower()
            and "query id is invalid" not in str(exc).lower()
        ):
            raise


def public_media_result(hit: dict[str, object]) -> bool:
    return (hit.get("source"), hit.get("kind")) in {
        ("soundcloud", "track"),
        ("ytmusic", "track"),
        ("ytmusic", "episode"),
        ("apple_podcasts", "episode"),
    }


HELP_TEXT = (
    "<b>MIKA NEXUS · Help</b>\n"
    "<b>Search:</b> /search query · /music query · /podcast query\n"
    "<b>Media:</b> send one or more public HTTP(S) links; approve each with Yes. "
    "Use /youtube, /tiktok, /instagram, or /twitter for platform guidance.\n"
    "<b>Jobs:</b> /queue · /cancel · /cancel JOB_ID · /status\n"
    "<b>Settings:</b> /settings; change search type and source filters in results.\n"
    "<b>Owner:</b> /admin\n"
    "Catalog links can find matching public recordings. Protected catalog audio is unavailable."
)


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.data_dir / "nexus.sqlite")
        self.store.recover()
        self.store.seed_access(settings.allowlist)
        self.searcher = Searcher()
        session = None
        if settings.bot_api_mode == "local":
            assert settings.local_bot_api_url is not None
            session = AiohttpSession(
                api=TelegramAPIServer.from_base(settings.local_bot_api_url, is_local=True)
            )
        self.bot = Bot(
            settings.bot_token, session=session, default=DefaultBotProperties(parse_mode="HTML")
        )
        self.worker = Worker(settings, self.store, self.bot)
        self.router = Router()
        self.router.message.register(self.message)
        self.router.callback_query.register(self.callback)
        self.dp = Dispatcher()
        self.dp.include_router(self.router)

    def authorized(self, user_id: int, chat_id: int, chat_type: str) -> bool:
        return allowed(
            user_id, chat_id, chat_type, self.store.allowlist(), self.settings.private_chats_only
        )

    async def message(self, m: Message) -> None:
        if not m.from_user or not self.authorized(m.from_user.id, m.chat.id, m.chat.type):
            return
        if not m.text:
            return
        text = m.text.strip()
        if text.startswith("/"):
            self.store.clear_query(m.from_user.id, m.chat.id)
            await self.command(m, text)
            return
        urls, extra = links(text, self.settings.max_links_per_message)
        if urls:
            if not self.store.rate(m.from_user.id, "links", 12):
                return
            for index, url in enumerate(urls):
                cid = self.store.confirm_create(
                    m.chat.id,
                    m.from_user.id,
                    m.message_id,
                    index,
                    url,
                    self.settings.confirmation_ttl_seconds,
                )
                if not self.store.claim_prompt(cid):
                    continue
                label = source(url)
                prompt = await m.reply(
                    f"🔗 <b>{html.escape(label.title())}</b> · لینک {index + 1} از {len(urls)}\n{html.escape(url[:300])}\n\nدریافت شود؟",
                    reply_markup=kb([[("✅ Yes", f"c:{cid}:y"), ("❌ No", f"c:{cid}:n")]]),
                )
                self.store.set_prompt(cid, prompt.message_id)
            if extra:
                await m.reply(
                    f"Only the first {self.settings.max_links_per_message} links were offered. Send the rest separately."
                )
            return
        mode = self.store.take_query(m.from_user.id, m.chat.id)
        if mode and 0 < len(text) <= 120:
            await self.begin_search(m, text, mode)

    async def command(self, m: Message, raw: str) -> None:
        if m.from_user is None:
            return
        word, _, arg = raw.partition(" ")
        cmd = word.split("@")[0].lower()
        arg = arg.strip()
        if cmd == "/start":
            await m.reply(
                "✨ <b>MIKA NEXUS</b>\nلینک عمومی بفرست یا برای پیدا کردن موسیقی و پادکست جست‌وجو کن.",
                reply_markup=kb(
                    [
                        [("🎵 Music", "m:music"), ("🎙 Podcast", "m:podcast")],
                        [("🔎 All search", "m:search"), ("📖 Help", "m:help")],
                        [("📥 Queue", "m:queue"), ("⚙ Settings", "m:settings")],
                    ]
                ),
            )
        elif cmd == "/help":
            await m.reply(HELP_TEXT)
        elif cmd in ("/search", "/music", "/podcast"):
            if arg:
                if len(arg) <= 120:
                    await self.begin_search(m, arg, cmd[1:])
            else:
                self.store.prompt_query(
                    m.from_user.id, m.chat.id, cmd[1:], self.settings.search_session_ttl_seconds
                )
                await m.reply("Send your search query in the next 10 minutes.")
        elif cmd in ("/twitter", "/instagram", "/tiktok", "/youtube"):
            await m.reply(
                f"Send a public {cmd[1:].title()} link. Every link asks for Yes/No confirmation first."
            )
        elif cmd == "/queue":
            jobs = self.store.jobs(m.from_user.id)
            await m.reply(
                "\n".join(f"<code>{j['id']}</code> · {j['state']}" for j in jobs) or "No jobs."
            )
        elif cmd == "/cancel":
            if not arg:
                active = [
                    j
                    for j in self.store.jobs(m.from_user.id)
                    if j["state"] in {"QUEUED", "RESOLVING", "DOWNLOADING", "VERIFYING"}
                ]
                await m.reply(
                    "Select a job to cancel:" if active else "No cancellable jobs.",
                    reply_markup=kb([[(j["id"], f"j:{j['id']}:cancel")] for j in active])
                    if active
                    else None,
                )
            elif self.store.cancel(arg, m.from_user.id):
                task = self.worker.tasks.get(arg)
                if task:
                    task.cancel()
                await m.reply("Cancelled.")
            else:
                await m.reply("Job not found or cannot be cancelled.")
        elif cmd == "/status":
            await m.reply(
                f"Jobs: {len(self.store.jobs(m.from_user.id))} recent · Active: {len(self.worker.tasks)}"
            )
        elif cmd == "/settings":
            pref = self.store.preferences(m.from_user.id)
            body = "Search filters are editable in each result view. Public audio is sent as MP3."
            if pref:
                body += "\nTypes: " + ", ".join(sorted(pref[0]))
                body += "\nSources: " + ", ".join(sorted(pref[1]))
            await m.reply(body)
        elif cmd == "/admin" and m.from_user.id == self.settings.owner_telegram_id:
            parts = arg.split()
            if len(parts) == 2 and parts[0] in {"add", "remove"} and parts[1].isdigit():
                token = self.store.propose_access(
                    m.from_user.id, m.chat.id, int(parts[1]), parts[0]
                )
                prompt = await m.reply(
                    f"Confirm {parts[0]} user {parts[1]}?",
                    reply_markup=kb(
                        [[("✅ Confirm", f"a:{token}:yes"), ("❌ Cancel", f"a:{token}:no")]]
                    ),
                )
                self.store.bind_admin_prompt(token, prompt.message_id)
            else:
                state = ", ".join(f"{k}: {v}" for k, v in self.searcher.health.items())
                await m.reply(
                    f"Owner diagnostics\nAllowed IDs: {len(self.store.allowlist())}\nActive jobs: {len(self.worker.tasks)}\nSources: {html.escape(state)}\nUse /admin add ID or /admin remove ID (confirmation required)."
                )

    async def begin_search(self, m: Message, query: str, mode: str) -> None:
        if m.from_user is None:
            return
        if not self.store.rate(m.from_user.id, "search", 10):
            await m.reply("Search limit reached. Try again shortly.")
            return
        kinds = (
            set(KINDS)
            if mode == "search"
            else (
                {"podcast", "episode"}
                if mode == "podcast"
                else {"track", "album", "playlist", "artist"}
            )
        )
        sources = {"apple", "apple_podcasts", "deezer", "soundcloud", "ytmusic"}
        pref = self.store.preferences(m.from_user.id)
        if pref:
            kinds, sources = pref
        sid = self.store.create_search(m.from_user.id, m.chat.id, query, kinds, sources)
        msg = await m.reply("🔎 در حال جست‌وجو…")
        self.store.set_search_message(sid, msg.message_id)
        await self.refresh_search(sid, m.from_user.id, m.chat.id)

    async def refresh_search(self, sid: str, user: int, chat: int) -> None:
        row = self.store.search(sid, user, chat)
        if not row:
            return
        kinds, sources = set(json.loads(row["kinds"])), set(json.loads(row["sources"]))
        if kinds and sources:
            hits, failures = await self.searcher.search(row["query"], kinds, sources)
        else:
            hits, failures = [], {}
        current = self.store.search(sid, user, chat)
        if not current or current["generation"] != row["generation"]:
            return
        self.store.save_results(sid, row["generation"], [h.data() for h in hits])
        await self.render_search(sid, user, chat, failures)

    async def render_search(
        self,
        sid: str,
        user: int,
        chat: int,
        failures: dict[str, str] | None = None,
        menu: str = "results",
        detail: int | None = None,
    ) -> None:
        row = self.store.search(sid, user, chat)
        if not row:
            return
        kinds, sources = set(json.loads(row["kinds"])), set(json.loads(row["sources"]))
        gen, page = row["generation"], row["page"]
        hits = self.store.results(sid, gen)
        head = f"🔎 <b>{html.escape(row['query'])}</b> · صفحه {page + 1}"
        buttons: list[list[tuple[str, str]]] = []
        if menu in ("types", "sources"):
            choices = KINDS if menu == "types" else SOURCES
            active = kinds if menu == "types" else sources
            for choice in choices:
                status = self.searcher.health.get(choice, "") if menu == "sources" else ""
                buttons.append(
                    [
                        (
                            f"{'✅' if choice in active else '⬜'} {choice} {status if status != 'ok' else ''}"[
                                :55
                            ],
                            f"s:{sid}:{'k' if menu == 'types' else 'p'}:{choice}",
                        )
                    ]
                )
            buttons.append([("◀ Back", f"s:{sid}:b")])
            body = "Choose types:" if menu == "types" else "Choose sources:"
        elif detail is not None and 0 <= detail < len(hits):
            h = hits[detail]
            body = f"🎵 {html.escape(h['source'])} · {html.escape(h['kind'])}\n<b>{html.escape(h['title'])}</b>\n👤 {html.escape(h['creator'])}\n{html.escape(h['url'][:700])}"
            if public_media_result(h):
                body += "\n\nPublic media may be available. Requesting it opens a separate Yes/No confirmation."
                buttons = [[("Request public media", f"s:{sid}:r:{detail}:{gen}")]]
            elif h["kind"] == "track" and h["source"] in {"spotify", "apple", "deezer"}:
                body += "\n\nFind public recordings matching this catalog track."
                buttons = [[("Find audio matches", f"s:{sid}:f:{detail}:{gen}")]]
            elif h["kind"] in {"album", "playlist"} and h["source"] in {
                "spotify",
                "apple",
                "deezer",
                "ytmusic",
            }:
                body += "\n\nBrowse available items in this collection."
                buttons = [[("Browse items", f"s:{sid}:l:{detail}:{gen}")]]
            elif h["kind"] == "podcast" and h["source"] == "apple_podcasts":
                body += "\n\nBrowse public episodes."
                buttons = [[("Browse episodes", f"s:{sid}:l:{detail}:{gen}")]]
            else:
                body += "\n\nCatalog information only. This source does not provide a downloadable full-audio file here."
            buttons.append([("◀ Back", f"s:{sid}:b")])
        else:
            current = hits[
                page * self.settings.search_page_size : (page + 1) * self.settings.search_page_size
            ]
            body = "\n".join(
                f"{i + 1:02}  {'🎵' if h['kind'] == 'track' else '🎙️' if h['kind'] in {'podcast', 'episode'} else '💿'} <b>{html.escape(h['title'][:70])}</b>\n      {html.escape(h['creator'][:35])} · {html.escape(h['source'])}"
                for i, h in enumerate(current, page * self.settings.search_page_size)
            )
            if not body:
                body = "No matching results. Select at least one type and source, or try another query."
            if failures:
                body += "\nUnavailable: " + html.escape(
                    ", ".join(f"{k} ({v})" for k, v in failures.items())[:300]
                )
            for offset in range(0, len(current), 5):
                buttons.append(
                    [
                        (
                            str(page * self.settings.search_page_size + i + 1),
                            f"s:{sid}:d:{page * self.settings.search_page_size + i}:{gen}",
                        )
                        for i in range(offset, min(offset + 5, len(current)))
                    ]
                )
            buttons.append([("🧩 Types", f"s:{sid}:types"), ("🌐 Sources", f"s:{sid}:sources")])
            buttons.append([("◀ Previous", f"s:{sid}:prev"), ("Next ▶", f"s:{sid}:next")])
            buttons.append([("🔄 Refresh", f"s:{sid}:refresh"), ("✖ Close", f"s:{sid}:close")])
        try:
            keyboard = kb(buttons)
            if detail is not None and 0 <= detail < len(hits) and hits[detail]["kind"] == "track":
                hit = hits[detail]
                keyboard.inline_keyboard.insert(
                    -1,
                    [InlineKeyboardButton(
                        text="📝 متن آهنگ", callback_data=f"s:{sid}:y:{detail}:{gen}"
                    )],
                )
                keyboard.inline_keyboard.insert(
                    -1,
                    [InlineKeyboardButton(text=label, url=target)
                     for label, target in listening_links(
                         str(hit["title"]), str(hit["creator"]),
                         str(hit["source"]), str(hit["url"])
                     )],
                )
            await self.bot.edit_message_text(
                head + "\n\n" + body,
                chat_id=chat,
                message_id=row["message_id"],
                reply_markup=keyboard,
            )
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def render_collection(self, cid: str, user: int, chat: int, page: int) -> None:
        row = self.store.collection(cid, user, chat)
        if not row:
            return
        items = json.loads(row["items"])
        page = max(0, min(page, (len(items) - 1) // 10))
        buttons = [
            [(f"{i + 1}. {items[i]['title'][:45]}", f"v:{cid}:d:{i}")]
            for i in range(page * 10, min((page + 1) * 10, len(items)))
        ]
        nav = []
        if page:
            nav.append(("◀ Previous", f"v:{cid}:p:{page - 1}"))
        if (page + 1) * 10 < len(items):
            nav.append(("Next ▶", f"v:{cid}:p:{page + 1}"))
        if nav:
            buttons.append(nav)
        await self.bot.edit_message_text(
            f"MIKA NEXUS · {html.escape(row['title'])}\nSelect one item:",
            chat_id=chat,
            message_id=row["message_id"],
            reply_markup=kb(buttons),
        )

    async def callback(self, q: CallbackQuery) -> None:
        if (
            not q.from_user
            or q.message is None
            or isinstance(q.message, InaccessibleMessage)
            or not self.authorized(q.from_user.id, q.message.chat.id, q.message.chat.type)
        ):
            await acknowledge(q, "Unavailable")
            return
        data = q.data or ""
        if data.startswith("m:"):
            await acknowledge(q)
            action = data[2:]
            if action in {"search", "music", "podcast"}:
                self.store.prompt_query(
                    q.from_user.id,
                    q.message.chat.id,
                    action,
                    self.settings.search_session_ttl_seconds,
                )
                await self.bot.send_message(
                    q.message.chat.id, "Send your search query in the next 10 minutes."
                )
            elif action == "help":
                await self.bot.send_message(q.message.chat.id, HELP_TEXT)
            elif action == "queue":
                jobs = self.store.jobs(q.from_user.id)
                await self.bot.send_message(
                    q.message.chat.id,
                    "\n".join(f"<code>{j['id']}</code> · {j['state']}" for j in jobs) or "No jobs.",
                )
            elif action == "settings":
                pref = self.store.preferences(q.from_user.id)
                body = (
                    "Search filters are editable in each result view. "
                    "Audio recordings are delivered as MP3 when requested."
                )
                if pref:
                    body += "\nTypes: " + ", ".join(sorted(pref[0]))
                    body += "\nSources: " + ", ".join(sorted(pref[1]))
                await self.bot.send_message(q.message.chat.id, body)
            return
        if data.startswith("a:"):
            await acknowledge(q)
            parts = data.split(":")
            if len(parts) != 3 or q.from_user.id != self.settings.owner_telegram_id:
                return
            if parts[2] == "yes":
                result = self.store.apply_access(
                    parts[1],
                    q.from_user.id,
                    self.settings.owner_telegram_id,
                    q.message.chat.id,
                    q.message.message_id,
                )
            else:
                self.store.db.execute(
                    "DELETE FROM admin_pending WHERE id=? AND owner_id=?",
                    (parts[1], q.from_user.id),
                )
                result = "Cancelled"
            await q.message.edit_text(html.escape(result))
            return
        if data.startswith("c:"):
            parts = data.split(":")
            if len(parts) != 3 or parts[2] not in {"y", "n"}:
                await acknowledge(q)
                return
            state, jid = self.store.decision(
                parts[1], q.from_user.id, q.message.chat.id, q.message.message_id, parts[2] == "y"
            )
            await acknowledge(q, "Expired" if state == "EXPIRED" else None)
            if state == "REJECTED":
                try:
                    await q.message.delete()
                except Exception:
                    try:
                        await q.message.edit_reply_markup(reply_markup=None)
                    except Exception:
                        pass
            elif state == "ACCEPTED":
                await q.message.edit_text(
                    "⏳ درخواست ثبت شد؛ در حال آماده‌سازی…",
                    reply_markup=kb([[("لغو ✕", f"j:{jid}:cancel")]]),
                )
            elif state == "EXPIRED":
                await q.message.edit_reply_markup(reply_markup=None)
            return
        if data.startswith("v:"):
            await acknowledge(q)
            parts = data.split(":")
            if len(parts) != 4:
                return
            cid, action, arg = parts[1:]
            row = self.store.collection(cid, q.from_user.id, q.message.chat.id)
            if not row or row["message_id"] != q.message.message_id:
                return
            items = json.loads(row["items"])
            if not arg.isdigit():
                return
            number = int(arg)
            if action == "p":
                await self.render_collection(cid, q.from_user.id, q.message.chat.id, number)
            elif action == "d" and 0 <= number < len(items):
                jid, created = self.store.select_collection(
                    cid, q.from_user.id, q.message.chat.id, number
                )
                if created and jid is not None:
                    status = await self.bot.send_message(
                        q.message.chat.id,
                        "⏳ مورد انتخابی در صف دریافت است…",
                        reply_markup=kb([[("Cancel", f"j:{jid}:cancel")]]),
                    )
                    self.store.bind_progress(jid, status.message_id)
            return
        if data.startswith("j:"):
            await acknowledge(q)
            parts = data.split(":")
            if (
                len(parts) == 3
                and parts[2] == "cancel"
                and self.store.cancel(parts[1], q.from_user.id)
            ):
                task = self.worker.tasks.get(parts[1])
                if task:
                    task.cancel()
                await q.message.edit_text("Cancelled")
            elif len(parts) == 3 and parts[2] == "audio":
                jid, created = self.store.create_derivative(
                    parts[1], q.from_user.id, q.message.chat.id, "audio"
                )
                if created and jid is not None:
                    if q.message.document or q.message.video:
                        await q.message.edit_reply_markup(reply_markup=None)
                        status = await self.bot.send_message(
                            q.message.chat.id, "🎵 در حال آماده‌سازی MP3…"
                        )
                        self.store.bind_progress(jid, status.message_id)
                    else:
                        await q.message.edit_text("🎵 در حال آماده‌سازی MP3…")
                        self.store.bind_progress(jid, q.message.message_id)
            return
        if not data.startswith("s:"):
            await acknowledge(q)
            return
        await acknowledge(q)
        parts = data.split(":")
        if len(parts) < 3:
            return
        sid, action = parts[1:3]
        row = self.store.search(sid, q.from_user.id, q.message.chat.id)
        if not row:
            expired = self.store.db.execute(
                "SELECT user_id,chat_id,message_id,expires FROM search_sessions WHERE id=?",
                (sid,),
            ).fetchone()
            if (
                expired
                and expired["user_id"] == q.from_user.id
                and expired["chat_id"] == q.message.chat.id
                and expired["message_id"] == q.message.message_id
                and expired["expires"] <= time.time()
            ):
                try:
                    await q.message.edit_text("Search expired. Send /search to start again.")
                except TelegramBadRequest:
                    pass
            return
        if row["message_id"] != q.message.message_id:
            return
        kinds, sources = set(json.loads(row["kinds"])), set(json.loads(row["sources"]))
        if action in ("k", "p") and len(parts) == 4:
            choices, active = (KINDS, kinds) if action == "k" else (SOURCES, sources)
            if parts[3] not in choices:
                return
            if parts[3] in active:
                active.remove(parts[3])
            else:
                active.add(parts[3])
            self.store.update_search(sid, kinds, sources)
            self.store.save_preferences(q.from_user.id, kinds, sources)
            await self.refresh_search(sid, q.from_user.id, q.message.chat.id)
            await self.render_search(
                sid, q.from_user.id, q.message.chat.id, menu="types" if action == "k" else "sources"
            )
        elif action in ("types", "sources"):
            await self.render_search(sid, q.from_user.id, q.message.chat.id, menu=action)
        elif (
            action == "d"
            and len(parts) == 5
            and parts[4].isdigit()
            and int(parts[4]) == row["generation"]
        ):
            await self.render_search(sid, q.from_user.id, q.message.chat.id, detail=int(parts[3]))
        elif (
            action == "y"
            and len(parts) == 5
            and parts[3].isdigit()
            and parts[4].isdigit()
            and int(parts[4]) == row["generation"]
        ):
            hit = self.store.result(sid, row["generation"], int(parts[3]))
            if not hit or hit.get("kind") != "track":
                return
            try:
                lyric = await verified_lyrics(str(hit["title"]), str(hit["creator"]))
            except Exception:
                lyric = None
            if lyric:
                title = html.escape(str(hit["title"])[:150])
                for offset in range(0, min(len(lyric), 12000), 3500):
                    await self.bot.send_message(
                        q.message.chat.id,
                        (f"📝 <b>{title}</b>\n" if offset == 0 else "")
                        + html.escape(lyric[offset : offset + 3500]),
                    )
            else:
                await self.bot.send_message(
                    q.message.chat.id, "متن تأییدشده‌ای برای این نسخه پیدا نشد."
                )
        elif (
            action == "l"
            and len(parts) == 5
            and parts[3].isdigit()
            and parts[4].isdigit()
            and int(parts[4]) == row["generation"]
        ):
            hit = self.store.result(sid, row["generation"], int(parts[3]))
            if not hit or hit.get("kind") not in {"album", "playlist", "podcast"}:
                return
            try:
                items, title = await self.worker._browse(clean_url(str(hit["url"])))
            except (ValueError, TimeoutError):
                await self.bot.send_message(q.message.chat.id, "Collection items are unavailable.")
                return
            if not items:
                await self.bot.send_message(q.message.chat.id, "No public collection items found.")
                return
            cid = self.store.create_collection(q.from_user.id, q.message.chat.id, title, items)
            prompt = await self.bot.send_message(q.message.chat.id, "Loading collection…")
            self.store.db.execute(
                "UPDATE collections SET message_id=? WHERE id=?", (prompt.message_id, cid)
            )
            await self.render_collection(cid, q.from_user.id, q.message.chat.id, 0)
        elif (
            action == "f"
            and len(parts) == 5
            and parts[3].isdigit()
            and parts[4].isdigit()
            and int(parts[4]) == row["generation"]
        ):
            hit = self.store.result(sid, row["generation"], int(parts[3]))
            if (
                not hit
                or hit.get("kind") != "track"
                or hit.get("source") not in {"spotify", "apple", "deezer"}
            ):
                return
            query = f"{hit['creator']} {hit['title']}".strip()
            alt_sid = self.store.create_search(
                q.from_user.id,
                q.message.chat.id,
                query,
                {"track"},
                {"soundcloud", "ytmusic"},
            )
            pending = await self.bot.send_message(
                q.message.chat.id, "Finding public audio matches…"
            )
            self.store.set_search_message(alt_sid, pending.message_id)
            hits, failures = await self.searcher.search(query, {"track"}, {"soundcloud", "ytmusic"})
            ranked = sorted(
                (
                    (
                        match_score(
                            hit["title"],
                            hit["creator"],
                            h.title,
                            h.creator,
                            hit.get("duration"),
                            h.duration,
                        ),
                        h,
                    )
                    for h in hits
                ),
                key=lambda item: item[0],
                reverse=True,
            )
            self.store.save_results(alt_sid, 0, [h.data() for score, h in ranked if score >= 0.48])
            await self.render_search(alt_sid, q.from_user.id, q.message.chat.id, failures)
        elif (
            action == "r"
            and len(parts) == 5
            and parts[3].isdigit()
            and parts[4].isdigit()
            and int(parts[4]) == row["generation"]
        ):
            index = int(parts[3])
            hit = self.store.result(sid, row["generation"], index)
            if not hit or not public_media_result(hit):
                return
            try:
                url = clean_url(str(hit["url"]))
            except ValueError:
                return
            if not self.store.rate(q.from_user.id, "links", 12):
                return
            cid = self.store.confirm_create(
                q.message.chat.id,
                q.from_user.id,
                q.message.message_id,
                index,
                url,
                self.settings.confirmation_ttl_seconds,
            )
            if self.store.claim_prompt(cid):
                prompt = await self.bot.send_message(
                    q.message.chat.id,
                    f"MIKA NEXUS · Confirm public media request\n<code>{html.escape(url[:300])}</code>",
                    reply_markup=kb([[("✅ Yes", f"c:{cid}:y"), ("❌ No", f"c:{cid}:n")]]),
                )
                self.store.set_prompt(cid, prompt.message_id)
        elif action in ("prev", "next"):
            page = max(0, row["page"] + (1 if action == "next" else -1))
            if page * self.settings.search_page_size < max(
                1, len(self.store.results(sid, row["generation"]))
            ):
                self.store.db.execute("UPDATE search_sessions SET page=? WHERE id=?", (page, sid))
            await self.render_search(sid, q.from_user.id, q.message.chat.id)
        elif action == "refresh":
            self.store.update_search(sid, kinds, sources)
            await self.refresh_search(sid, q.from_user.id, q.message.chat.id)
        elif action == "close":
            self.store.db.execute("DELETE FROM search_sessions WHERE id=?", (sid,))
            await q.message.edit_text("Search closed.")
        else:
            await self.render_search(sid, q.from_user.id, q.message.chat.id)

    async def run(self) -> None:
        await self.bot.set_my_commands(
            [
                BotCommand(command=x, description=y)
                for x, y in (
                    ("start", "Open MIKA NEXUS"),
                    ("help", "How to use"),
                    ("search", "Search music and podcasts"),
                    ("music", "Search music"),
                    ("podcast", "Search podcasts"),
                    ("twitter", "Twitter link help"),
                    ("instagram", "Instagram link help"),
                    ("tiktok", "TikTok link help"),
                    ("youtube", "YouTube link help"),
                    ("settings", "Preferences"),
                    ("queue", "My jobs"),
                    ("status", "Status"),
                    ("cancel", "Cancel my job"),
                )
            ]
        )
        task = asyncio.create_task(self.worker.loop())
        try:
            await self.dp.start_polling(self.bot)
        finally:
            await self.worker.stop()
            task.cancel()
            await self.bot.session.close()
