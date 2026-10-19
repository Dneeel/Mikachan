"""Native Telegram interface. All actions recheck actor, chat and session ownership."""

import asyncio
import html
import json
import os
import secrets
import shutil
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InaccessibleMessage,
    Message,
)

from .config import Settings
from .jobs import Worker
from .catalog import _Meta
from .search import KINDS, SOURCES, Searcher
from .matching import match_score
from .music_ui import listening_links, verified_lyrics
from .security import allowed, clean_url, collection, links, source, validate_dns
from .store import Store
from .community import Community, CommunityStore
from .utilities import Utilities, _market_card
from .market_prices import parse_asset_query
from .personality import Mode, mode_for, response
from .follow import (FollowService, FEATURES, Target, command_target, confirm_panel,
                     creator_from_post, profile_target, public_items,
                     resolve_creator_from_post, public_profile, settings_panel, SOCIAL_PLATFORMS)


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def youtube_video_url(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path.lower()
    return (source(url) == "youtube" and not collection(url)
            and parts.hostname != "music.youtube.com"
            and (parts.hostname == "youtu.be" or
                 path == "/watch" or path.startswith(("/shorts/", "/live/"))))


async def profile_photo(url: str) -> BufferedInputFile:
    """Fetch a bounded public avatar so Telegram need not access a signed CDN URL."""
    current = clean_url(url)
    async with httpx.AsyncClient(timeout=9, follow_redirects=False) as client:
        for _ in range(4):
            await asyncio.wait_for(asyncio.to_thread(validate_dns, current), 3)
            async with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("avatar redirect has no destination")
                    current = clean_url(urljoin(current, location))
                    continue
                response.raise_for_status()
                mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if mime not in {"image/jpeg", "image/png"}:
                    raise ValueError("avatar is not a supported photo")
                content = bytearray()
                async for chunk in response.aiter_bytes(65536):
                    content.extend(chunk)
                    if len(content) > 8 * 1024 * 1024:
                        raise ValueError("avatar exceeds photo limit")
                data = bytes(content)
                if not (data.startswith(b"\xff\xd8\xff") if mime == "image/jpeg"
                        else data.startswith(b"\x89PNG\r\n\x1a\n")):
                    raise ValueError("avatar content does not match image type")
                return BufferedInputFile(data, filename="profile.jpg" if mime == "image/jpeg"
                                         else "profile.png")
    raise ValueError("too many avatar redirects")


def download_prompt_markup(cid: str, url: str) -> InlineKeyboardMarkup:
    youtube_video = youtube_video_url(url)
    rows = [[("✅ Yes", f"c:{cid}:{'q' if youtube_video else 'y'}"),
             ("❌ No", f"c:{cid}:n")]]
    if source(url) in SOCIAL_PLATFORMS and profile_target(url) is None:
        rows.append([("👤 Profile", f"c:{cid}:p")])
    return kb(rows)


def collection_view(
    cid: str, title: str, items: Sequence[Mapping[str, object]], page: int,
    context: dict[str, object],
) -> tuple[str, InlineKeyboardMarkup]:
    page = max(0, min(page, (len(items) - 1) // 10))
    rows: list[list[tuple[str, str]]] = []
    for index in range(page * 10, min((page + 1) * 10, len(items))):
        item = items[index]
        name = str(item.get("title") or "Untitled")[:38]
        artist = str(item.get("artist") or "")[:22]
        rows.append([(f"{index + 1}. {name}{' · ' + artist if artist else ''}"[:64],
                      f"v:{cid}:q:{index}")])
    nav = []
    if page:
        nav.append(("◀ Previous", f"v:{cid}:p:{page - 1}"))
    if (page + 1) * 10 < len(items):
        nav.append(("Next ▶", f"v:{cid}:p:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("⬇️ Download all", f"v:{cid}:q:all")])
    if context.get("search_sid"):
        rows.append([("◀ Back to result", f"v:{cid}:back:0")])
    rows.append([("✖ Close", f"v:{cid}:close:0")])
    markup = kb(rows)
    owner_url = str(context.get("owner_url") or "")
    source_url = str(context.get("source_url") or "")
    links = []
    if owner_url:
        links.append(InlineKeyboardButton(text="👤 Creator", url=owner_url))
    if source_url:
        links.append(InlineKeyboardButton(text="🔗 Link", url=source_url))
    if links:
        markup.inline_keyboard.append(links)
    target = profile_target(source_url)
    if target:
        markup.inline_keyboard.append([
            InlineKeyboardButton(text="👀 Follow creator", callback_data=f"v:{cid}:follow:0")])
    text = f"📚 <b>{html.escape(title[:150])}</b>\n{len(items)} items · Page {page + 1}"
    return text, markup


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
        ("youtube", "track"),
        ("ytmusic", "episode"),
        ("apple_podcasts", "episode"),
        ("spotify", "track"),
        ("spotify", "episode"),
        ("apple", "track"),
        ("deezer", "track"),
    }


HELP_TEXT = (
    "◈ <b>Mikachan</b>\nChoose a command. Eligible links always ask before downloading.\n\n"
    "⬇️ <b>Media</b>\n"
    "/search · /music · /podcast · /twitter\n"
    "/follow · /following · /queue · /cancel\n\n"
    "👥 <b>Community</b>\n"
    "/fal · /poem · /quote · /dice\n"
    "Groups: /rules · /note · /groupstats · /groupsettings\n\n"
    "🛠 <b>Utilities</b>\n"
    "/qr · /scan · /ocr\n"
    "/weather CITY · /rate ASSET IRT · /now [CITY]\n"
    "/apod · /translate en|fa TEXT\n\n"
    "💱 Toman quotes: send exactly دلار, سکه, طلا, تتر or ارز.\n"
    "For file tools, reply to the file with a command."
)


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.data_dir / "mikachan.sqlite")
        self.store.recover()
        self._clear_interrupted_media()
        self.store.seed_access(settings.allowlist)
        self.store.seed_groups(settings.groups)
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
        self.follow = FollowService(self.store, self.bot, settings.private_chats_only)
        community_store = CommunityStore(self.store.db)
        community_store.seed_hafez(Path(__file__).parent / "content/hafez.json")
        self.community = Community(self.bot, community_store, settings.owner_telegram_id)
        self.utilities = Utilities(self.bot, settings, self.store.db)
        self.router = Router()
        self.router.message.register(self.message)
        self.router.callback_query.register(self.callback)
        self.dp = Dispatcher()
        self.dp.include_router(self.router)

    def _clear_interrupted_media(self) -> None:
        """A newly started worker has no active jobs; discard crash leftovers."""
        roots = [self.settings.data_dir / "jobs"]
        runtime_temp = self.settings.data_dir.parent / "tmp/runtime"
        if os.environ.get("TMPDIR") == str(runtime_temp):
            roots.append(runtime_temp)
        for root in roots:
            if root.is_symlink():
                raise RuntimeError("Transient media directory cannot be a symlink")
            if not root.is_dir():
                continue
            for item in root.iterdir():
                if item.is_symlink() or item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)

    def authorized(self, user_id: int, chat_id: int, chat_type: str) -> bool:
        return allowed(
            user_id, chat_id, chat_type, self.store.allowlist(), self.settings.private_chats_only,
            self.store.groups()
        )

    async def message(self, m: Message) -> None:
        if getattr(m, "new_chat_members", None) and m.chat.id in self.store.groups():
            await self.community.welcome(m, self.store.allowlist())
        if not m.from_user or not self.authorized(m.from_user.id, m.chat.id, m.chat.type):
            return
        if not m.text:
            return
        text = m.text.strip()
        if text.startswith("/") and "@" in text.split(" ", 1)[0]:
            target_bot = text.split(" ", 1)[0].split("@", 1)[1]
            if getattr(self, "bot_username", "") and target_bot.lower() != self.bot_username.lower():
                return
        if m.chat.type in {"group", "supergroup"}:
            command_name = text.split(" ", 1)[0].split("@")[0].lower().lstrip("/")
            explicit_text = (text in {"فال", "شعر", "ارز"} or parse_asset_query(text) is not None or
                             text.startswith(("ارز ", "ترجمه ", "هواشناسی ", "اکنون ")) or
                             text in {"هواشناسی", "اکنون"})
            builtin = command_name in {
                "help", "search", "music", "podcast", "pod", "twitter", "instagram",
                "follow", "following",
                "tiktok", "youtube", "settings", "queue", "status", "cancel", "admin",
                "fal", "poem", "quote", "dice", "nickname", "rules", "setrules",
                "note", "setnote", "delnote", "setcmd", "delcmd", "welcome", "del",
                "mute", "modlog", "groupstats", "groupsettings", "qr", "scan", "ocr",
                "weather", "rate", "now", "apod", "translate"}
            custom = text.startswith("/") and self.community.store.item(
                "group_commands", m.chat.id, command_name) is not None
            if explicit_text or text.startswith("/") and (builtin or custom):
                self.community.store.use(m.chat.id, m.from_user.id, "request")
        if (m.chat.type in {"group", "supergroup"} and
                not self.community.store.enabled(m.chat.id, "utilities") and
                (text == "ارز" or parse_asset_query(text) is not None or
                 text.startswith(("ارز ", "ترجمه ", "هواشناسی ", "اکنون ")) or
                 text in {"هواشناسی", "اکنون"})):
            return
        if text == "اکنون" or text.startswith("اکنون "):
            await self.utilities.command(m, "/now", text.removeprefix("اکنون").strip())
            return
        if text == "هواشناسی" or text.startswith("هواشناسی "):
            await self.utilities.command(m, "/weather", text.removeprefix("هواشناسی").strip())
            return
        if text in {"فال", "شعر"}:
            await self.community.command(m, "/fal" if text == "فال" else "/poem", "")
            return
        if text == "ارز":
            try:
                await m.reply(_market_card(await self.utilities.market_prices.report(
                    ("USD", "EUR", "AED"))))
            except (ValueError, httpx.HTTPError) as exc:
                await m.reply(html.escape(str(exc)[:300]))
            return
        asset_query = parse_asset_query(text)
        if asset_query:
            symbol, amount = asset_query
            await self.utilities.command(m, "/rate", f"{symbol} IRT" if amount == 1 else
                                         f"{amount} {symbol} IRT")
            return
        if text.startswith("ارز "):
            await self.utilities.command(m, "/rate", text[4:].strip())
            return
        if text == "ترجمه انگلیسی":
            await self.utilities.command(m, "/translate", "en")
            return
        if text == "ترجمه فارسی":
            await self.utilities.command(m, "/translate", "fa")
            return
        if text.startswith("ترجمه انگلیسی "):
            await self.utilities.command(m, "/translate", "en " + text[len("ترجمه انگلیسی "):])
            return
        if text.startswith("ترجمه فارسی "):
            await self.utilities.command(m, "/translate", "fa " + text[len("ترجمه فارسی "):])
            return
        if text.startswith("ترجمه "):
            await m.reply("Specify a target: ترجمه انگلیسی TEXT or ترجمه فارسی TEXT.")
            return
        if text.startswith("/"):
            self.store.clear_query(m.from_user.id, m.chat.id)
            await self.command(m, text)
            return
        if hasattr(self, "community") and await self.community.search_reply(m):
            return
        if hasattr(self, "utilities") and hasattr(self.utilities, "now") and \
                await self.utilities.now.reply(m):
            return
        urls, extra = links(text, self.settings.max_links_per_message)
        if urls:
            if m.chat.type in {"group", "supergroup"}:
                self.community.store.use(m.chat.id, m.from_user.id, "request")
            if not self.store.rate(m.from_user.id, "links", 12):
                return
            for index, url in enumerate(urls):
                target = profile_target(url)
                if target:
                    await self.show_profile(m.chat.id, m.from_user.id, target)
                    continue
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
                    f"🔗 <b>{html.escape(label.title())}</b> · Link {index + 1} of {len(urls)}\n{html.escape(url[:300])}\n\nDownload this item?",
                    reply_markup=download_prompt_markup(cid, url),
                )
                self.store.set_prompt(cid, prompt.message_id)
            if extra:
                await m.reply(
                    f"Only the first {self.settings.max_links_per_message} links were offered. Send the rest separately."
                )
            return
        mode = self.store.take_query(m.from_user.id, m.chat.id)
        if mode and 0 < len(text) <= 120:
            if m.chat.type in {"group", "supergroup"}:
                self.community.store.use(m.chat.id, m.from_user.id, "request")
            await self.begin_search(m, text, mode)

    async def command(self, m: Message, raw: str) -> None:
        if m.from_user is None:
            return
        word, _, arg = raw.partition(" ")
        cmd = word.split("@")[0].lower()
        arg = arg.strip()
        if await self.community.command(m, cmd, arg):
            return
        if (m.chat.type in {"group", "supergroup"} and
                not self.community.store.enabled(m.chat.id, "utilities") and
                cmd in {"/qr", "/scan", "/ocr", "/weather", "/rate", "/now",
                        "/apod", "/translate"}):
            return
        if await self.utilities.command(m, cmd, arg):
            return
        if cmd == "/help":
            personality_mode = (mode_for(m.from_user.id, self.store.allowlist(), self.settings.mommy_ids)
                    if m.chat.type == "private" else Mode.NORMAL)
            extra_help = ("\n\n🛡 Group admins: /welcome · /setrules · /setnote · "
                          "/setcmd · /nickname · /del · /mute · /modlog"
                          if m.chat.type in {"group", "supergroup"} else "")
            if m.from_user.id == self.settings.owner_telegram_id:
                extra_help += "\n🔐 Owner: /admin"
            await m.reply(HELP_TEXT + extra_help + "\n\n" + response("ready", personality_mode))
        elif cmd == "/follow":
            target = command_target(arg)
            if target is None:
                await m.reply("Use /follow instagram|twitter|youtube|tiktok HANDLE, or /follow PROFILE_URL.")
            else:
                await self.show_follow(m.chat.id, m.from_user.id, target)
        elif cmd == "/following":
            await self.show_following(m.chat.id, m.from_user.id, m.chat.type)
        elif cmd in ("/search", "/music", "/podcast", "/pod"):
            mode = "podcast" if cmd == "/pod" else cmd[1:]
            if arg:
                if len(arg) <= 120:
                    await self.begin_search(m, arg, mode)
            else:
                self.store.prompt_query(
                    m.from_user.id, m.chat.id, mode, self.settings.search_session_ttl_seconds
                )
                await m.reply("Send your search query in the next 10 minutes.")
        elif cmd in ("/twitter", "/instagram", "/tiktok", "/youtube"):
            target = profile_target(arg) if arg.startswith(("http://", "https://")) else command_target(
                f"{cmd[1:]} {arg}") if arg else None
            if target and target.platform == cmd[1:]:
                await self.show_profile(m.chat.id, m.from_user.id, target)
            else:
                await m.reply(
                    f"Send a public {cmd[1:].title()} link or use {cmd} @handle to open Follow."
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
            body = ("⚙️ <b>Your settings</b>\n"
                    "Search filters · change them in a result view\n"
                    "Public audio · MP3")
            if pref:
                body += "\nTypes · " + html.escape(", ".join(sorted(pref[0])))
                body += "\nSources · " + html.escape(", ".join(sorted(pref[1])))
            body += "\nLocal time · /now\nQueue · /queue"
            await m.reply(body)
        elif cmd == "/admin" and m.from_user.id == self.settings.owner_telegram_id:
            parts = arg.split()
            if len(parts) == 3 and parts[0] == "provider":
                try:
                    self.utilities.set_provider(parts[1], parts[2])
                except ValueError as exc:
                    await m.reply(html.escape(str(exc)))
                else:
                    await m.reply("Price provider saved.")
                return
            group_change = (len(parts) == 3 and parts[0] == "group" and
                            parts[1] in {"add", "remove"} and
                            parts[2].startswith("-") and parts[2][1:].isdigit())
            user_change = (len(parts) == 2 and parts[0] in {"add", "remove"} and
                           parts[1].isdigit())
            if group_change or user_change:
                target = int(parts[2] if group_change else parts[1])
                action = "group_" + parts[1] if group_change else parts[0]
                token = self.store.propose_access(
                    m.from_user.id, m.chat.id, target, action
                )
                prompt = await m.reply(
                    f"Confirm {html.escape(action)} {target}?",
                    reply_markup=kb(
                        [[("✅ Confirm", f"a:{token}:yes"), ("❌ Cancel", f"a:{token}:no")]]
                    ),
                )
                self.store.bind_admin_prompt(token, prompt.message_id)
            else:
                state = ", ".join(f"{k}: {v}" for k, v in self.searcher.health.items())
                await m.reply(
                    f"Owner diagnostics\nAllowed users: {len(self.store.allowlist())}\nAllowed groups: {len(self.store.groups())}\nActive jobs: {len(self.worker.tasks)}\nSources: {html.escape(state)}\nPrice providers: fiat {self.utilities.provider('fiat')}, crypto {self.utilities.provider('crypto')}\nUse /admin add|remove USER_ID or /admin group add|remove GROUP_ID (confirmation required). /admin provider fiat|crypto NAME changes the fixed price provider."
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
        sources = {"apple", "apple_podcasts", "deezer", "soundcloud", "ytmusic", "youtube"}
        if self.searcher.health["spotify"] != "credentials required":
            sources.add("spotify")
        pref = self.store.preferences(m.from_user.id)
        if pref:
            saved_kinds, sources = pref
            if mode == "search":
                kinds = saved_kinds
            else:
                narrowed = kinds & saved_kinds
                if narrowed:
                    kinds = narrowed
        sid = self.store.create_search(m.from_user.id, m.chat.id, query, kinds, sources)
        msg = await m.reply("🔎 Searching…")
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
        head = f"🔎 <b>{html.escape(row['query'])}</b> · Page {page + 1}"
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
            icon = "🎙️" if h["kind"] in {"podcast", "episode"} else "🎵"
            body = f"{icon} <b>{html.escape(h['title'])}</b>\n👤 {html.escape(h['creator'])}"
            if public_media_result(h):
                buttons = [[("⬇️ Download", f"s:{sid}:r:{detail}:{gen}")]]
            elif h["kind"] in {"album", "playlist"} and h["source"] in {
                "spotify",
                "apple",
                "deezer",
                "ytmusic",
            }:
                body += "\n\nBrowse available items in this collection."
                buttons = [[("Browse items", f"s:{sid}:l:{detail}:{gen}")]]
            elif h["kind"] == "podcast" and h["source"] in {"apple_podcasts", "spotify"}:
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
            if kinds <= {"podcast", "episode"}:
                buttons.append([
                    ("🎙 Shows", f"s:{sid}:quick:podcast"),
                    ("🎧 Episodes", f"s:{sid}:quick:episode"),
                ])
            buttons.append([("🧩 Types", f"s:{sid}:types"), ("🌐 Sources", f"s:{sid}:sources")])
            buttons.append([("◀ Previous", f"s:{sid}:prev"), ("Next ▶", f"s:{sid}:next")])
            buttons.append([("🔄 Refresh", f"s:{sid}:refresh"), ("✖ Close", f"s:{sid}:close")])
        try:
            keyboard = kb(buttons)
            if detail is not None and 0 <= detail < len(hits):
                keyboard.inline_keyboard.insert(
                    -1,
                    [InlineKeyboardButton(text="🔗 Link", url=str(hits[detail]["url"]))],
                )
            if detail is not None and 0 <= detail < len(hits) and hits[detail]["kind"] == "track":
                hit = hits[detail]
                keyboard.inline_keyboard.insert(
                    -1,
                    [InlineKeyboardButton(
                        text="📝 Lyrics", callback_data=f"s:{sid}:y:{detail}:{gen}"
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
        body, markup = collection_view(
            cid, str(row["title"]), items, page, self.store.collection_context(cid)
        )
        try:
            await self.bot.edit_message_text(
                body,
                chat_id=chat,
                message_id=row["message_id"],
                reply_markup=markup,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def show_follow(self, chat: int, user: int, target: Target) -> None:
        token = self.follow.db.session(user, chat, target)
        body, markup = confirm_panel(target, token)
        prompt = await self.bot.send_message(chat, body, reply_markup=markup)
        self.follow.db.bind(token, prompt.message_id)

    async def show_following(self, chat: int, user: int, chat_type: str,
                             message_id: int | None = None) -> None:
        rows = self.follow.db.db.execute(
            "SELECT id,platform,handle,chat_id FROM follows WHERE user_id=? "
            + ("" if chat_type == "private" else "AND chat_id=? ")
            + "ORDER BY platform,handle",
            (user,) if chat_type == "private" else (user, chat),
        ).fetchall()
        body = "👀 <b>Following</b>\nChoose a profile to manage."
        if not rows:
            body += "\nNo profiles yet. Use /follow to begin."
        buttons = [[(f"👤 {row['platform'].title()} · @{row['handle'][:28]}",
                     f"fm:{row['id']}:open")] for row in rows]
        buttons.append([("✖ Close", "fm:close:close")])
        if message_id is None:
            sent = await self.bot.send_message(chat, body, reply_markup=kb(buttons))
            self.follow.db.bind_menu(user, chat, sent.message_id)
        else:
            await self.bot.edit_message_text(body, chat_id=chat, message_id=message_id,
                                             reply_markup=kb(buttons))
            self.follow.db.bind_menu(user, chat, message_id)

    async def show_profile(self, chat: int, user: int, target: Target) -> None:
        token = self.follow.db.profile_panel(user, chat, target)
        rows = [
            [("👀 Follow", f"p:{token}:follow")],
            [("⬇️ Latest post", f"p:{token}:latest")],
        ]
        if target.platform == "youtube":
            rows.append([("▶️ Latest Short", f"p:{token}:shorts")])
        if target.platform in {"instagram", "tiktok"}:
            rows.append([("👤 Photo & bio", f"p:{token}:card"),
                         ("⭕ Stories", f"p:{token}:stories")])
        markup = kb(rows)
        markup.inline_keyboard.append([InlineKeyboardButton(text="🔗 Open profile", url=target.url)])
        body = (f"👤 <b>@{html.escape(target.handle)}</b> · {target.platform.title()}\n"
                "Public profile · Choose an action below.")
        prompt = None
        if hasattr(self.bot, "send_photo"):
            details = {"name": "", "bio": "", "avatar": ""}
            try:
                await asyncio.wait_for(asyncio.to_thread(validate_dns, target.url), 3)
                async with httpx.AsyncClient(timeout=6, follow_redirects=False) as client:
                    response = await client.get(target.url,
                                                headers={"User-Agent": "facebookexternalhit/1.1"})
                    response.raise_for_status()
                parser = _Meta()
                parser.feed(response.text[:500_000])
                details = {"name": parser.data.get("og:title", ""),
                           "bio": parser.data.get("og:description", ""),
                           "avatar": parser.data.get("og:image", "")}
            except (ValueError, TimeoutError, OSError, httpx.HTTPError):
                pass
            if not details.get("avatar") and target.platform != "youtube":
                try:
                    details = await asyncio.wait_for(
                        public_profile(target, display_avatar=True), 7)
                except (ValueError, TimeoutError, OSError, httpx.HTTPError):
                    pass
            try:
                body = (f"👤 <b>{html.escape(details.get('name') or target.handle)}</b>\n"
                        f"@{html.escape(target.handle)} · {target.platform.title()}\n\n"
                        f"{html.escape(details.get('bio', '')[:700])}")
                if details.get("avatar"):
                    avatar = await profile_photo(details["avatar"])
                    prompt = await self.bot.send_photo(chat, avatar,
                                                       caption=body[:1024], reply_markup=markup)
            except (ValueError, TimeoutError, OSError, TelegramBadRequest, httpx.HTTPError):
                pass
        if prompt is None:
            prompt = await self.bot.send_message(chat, body, reply_markup=markup)
        self.follow.db.bind_profile_panel(token, prompt.message_id)

    async def show_stories(self, chat: int, user: int, target: Target) -> None:
        try:
            stories = await public_items(target, "stories")
        except (ValueError, TimeoutError, OSError):
            stories = []
        if not stories:
            await self.bot.send_message(chat, "No accessible public stories found.")
            return
        items = [{"title": description or f"Story {index + 1}", "url": url,
                  "kind": "story"} for index, (_, url, description) in enumerate(stories)]
        cid = self.store.create_collection(user, chat, f"@{target.handle} · Stories", items,
                                           {"source_url": target.url, "kind": target.platform})
        prompt = await self.bot.send_message(chat, "Loading stories…")
        self.store.db.execute("UPDATE collections SET message_id=? WHERE id=?",
                              (prompt.message_id, cid))
        await self.render_collection(cid, user, chat, 0)

    async def profile_callback(self, q: CallbackQuery, data: str) -> None:
        parts = data.split(":")
        if len(parts) != 3:
            await acknowledge(q)
            return
        row = self.follow.db.get_profile_panel(parts[1], q.from_user.id,
                                               q.message.chat.id, q.message.message_id)
        if not row:
            await acknowledge(q, "Profile panel expired")
            return
        target = Target(row["platform"], row["handle"], row["url"])
        action = parts[2]
        if action in {"card", "stories", "latest", "shorts"} and not self.store.rate(
                q.from_user.id, "profile", 8):
            await acknowledge(q, "Please wait before checking more profiles")
            return
        await acknowledge(q)
        if action == "follow":
            await self.show_follow(q.message.chat.id, q.from_user.id, target)
        elif action == "card" and target.platform in {"instagram", "tiktok"}:
            jid = self.store.create_job(q.from_user.id, q.message.chat.id, target.url)
            prompt = await self.bot.send_message(q.message.chat.id, "⏳ Checking public profile…")
            self.store.bind_progress(jid, prompt.message_id)
        elif action == "stories" and target.platform in {"instagram", "tiktok"}:
            await self.show_stories(q.message.chat.id, q.from_user.id, target)
        elif action in {"latest", "shorts"}:
            try:
                items = await public_items(target, "shorts" if action == "shorts" else "posts")
            except (ValueError, TimeoutError, OSError):
                items = []
            if not items:
                await self.bot.send_message(q.message.chat.id, "No accessible public posts found.")
                return
            item_url = items[0][1]
            cid = self.store.confirm_create(q.message.chat.id, q.from_user.id,
                                            q.message.message_id, secrets.randbits(31), item_url,
                                            self.settings.confirmation_ttl_seconds)
            if self.store.claim_prompt(cid):
                prompt = await self.bot.send_message(
                    q.message.chat.id,
                    f"🔗 <b>Latest {html.escape(items[0][0])}</b>\n"
                    f"{html.escape(items[0][2][:250])}\n\nDownload this item?",
                    reply_markup=download_prompt_markup(cid, item_url))
                self.store.set_prompt(cid, prompt.message_id)

    async def follow_callback(self, q: CallbackQuery, data: str) -> None:
        parts = data.split(":")
        if len(parts) < 3:
            await acknowledge(q)
            return
        token, action = parts[1:3]
        row = self.follow.db.get_session(
            token, q.from_user.id, q.message.chat.id, q.message.message_id)
        if not row:
            await acknowledge(q, "Follow panel expired")
            return
        if action == "open" and row["stage"] == "confirm":
            self.follow.db.db.execute("DELETE FROM follow_sessions WHERE id=?", (token,))
            await acknowledge(q)
            await self.show_follow(q.message.chat.id, q.from_user.id,
                                   Target(row["platform"], row["handle"], row["url"]))
            return
        if action == "no":
            self.follow.db.db.execute("DELETE FROM follow_sessions WHERE id=?", (token,))
            await acknowledge(q)
            await q.message.edit_text("Settings closed. Follow unchanged."
                                      if row["stage"].startswith("edit:") else "Follow cancelled.")
            return
        if action == "yes" and row["stage"] == "confirm":
            self.follow.db.db.execute("UPDATE follow_sessions SET stage='settings' WHERE id=?", (token,))
        elif row["stage"] != "settings" and not row["stage"].startswith("edit:"):
            await acknowledge(q)
            return
        elif action == "t" and len(parts) == 4 and parts[3] in FEATURES[row["platform"]]:
            chosen = set(json.loads(row["features"]))
            chosen.symmetric_difference_update({parts[3]})
            self.follow.db.db.execute("UPDATE follow_sessions SET features=? WHERE id=?",
                                      (json.dumps(sorted(chosen)), token))
        elif action == "m" and len(parts) == 4 and parts[3] in {"media", "notice"}:
            self.follow.db.db.execute("UPDATE follow_sessions SET mode=? WHERE id=?", (parts[3], token))
        elif action == "d" and len(parts) == 4 and parts[3] in {"dm", "here"}:
            self.follow.db.db.execute("UPDATE follow_sessions SET destination=? WHERE id=?",
                                      (parts[3], token))
        elif action == "save":
            if not json.loads(row["features"]):
                await acknowledge(q, "Select at least one category")
                return
            if row["destination"] == "dm" and q.message.chat.id != q.from_user.id:
                try:
                    await self.bot.send_message(q.from_user.id, "👀 Follow notifications will arrive here.")
                except (TelegramBadRequest, TelegramForbiddenError):
                    await acknowledge(q, "Open a private chat with this bot first")
                    return
            try:
                self.follow.db.save(row)
            except ValueError as exc:
                await acknowledge(q, str(exc))
                return
            await acknowledge(q)
            await q.message.edit_text(
                (f"✅ Settings saved for @{html.escape(row['handle'])}."
                 if row["stage"].startswith("edit:") else
                 f"✅ Following @{html.escape(row['handle'])}. Public updates will be checked "
                 "about every 15 minutes. Use /following to manage."))
            return
        else:
            await acknowledge(q)
            return
        updated = self.follow.db.get_session(token, q.from_user.id, q.message.chat.id,
                                              q.message.message_id)
        await acknowledge(q)
        body, markup = settings_panel(updated)
        try:
            await q.message.edit_text(body, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def callback(self, q: CallbackQuery) -> None:
        if (
            not q.from_user
            or q.message is None
            or isinstance(q.message, InaccessibleMessage)
            or not self.authorized(q.from_user.id, q.message.chat.id, q.message.chat.type)
        ):
            await acknowledge(q)
            return
        data = q.data or ""
        if hasattr(self, "utilities") and hasattr(self.utilities, "now") and \
                await self.utilities.now.callback(q, data):
            return
        if hasattr(self, "community") and await self.community.callback(q, data):
            return
        if data.startswith("p:"):
            await self.profile_callback(q, data)
            return
        if data.startswith("f:"):
            await self.follow_callback(q, data)
            return
        if data.startswith("fm:"):
            parts = data.split(":")
            if len(parts) != 3:
                await acknowledge(q)
                return
            if not self.follow.db.menu_owned(q.from_user.id, q.message.chat.id,
                                             q.message.message_id):
                await acknowledge(q, "This menu belongs to another user or has expired")
                return
            follow_id, action = parts[1:]
            if action == "close":
                self.follow.db.db.execute("DELETE FROM follow_menus WHERE chat_id=? AND message_id=?",
                                          (q.message.chat.id, q.message.message_id))
                await acknowledge(q)
                await q.message.edit_text("Following menu closed.")
                return
            if action == "back":
                await acknowledge(q)
                await self.show_following(q.message.chat.id, q.from_user.id,
                                          q.message.chat.type, q.message.message_id)
                return
            row = self.follow.db.db.execute(
                "SELECT * FROM follows WHERE id=? AND user_id=?", (follow_id, q.from_user.id)
            ).fetchone()
            if not row or (q.message.chat.type != "private" and row["chat_id"] != q.message.chat.id):
                await acknowledge(q, "Follow unavailable")
                return
            if action == "open":
                selected = ", ".join(json.loads(row["features"])) or "none"
                body = (f"👀 <b>@{html.escape(row['handle'])}</b> · {row['platform'].title()}\n"
                        f"Watching: {html.escape(selected)}\n"
                        f"Delivery: {html.escape(row['mode'])}")
                markup = kb([[("⚙️ Settings", f"fm:{follow_id}:edit"),
                              ("🔄 Check now", f"fm:{follow_id}:check")],
                             [("✕ Unfollow", f"fm:{follow_id}:remove")],
                             [("◀ Back", f"fm:{follow_id}:back"),
                              ("✖ Close", "fm:close:close")]])
                await acknowledge(q)
                await q.message.edit_text(body, reply_markup=markup)
                return
            if action == "edit":
                token = self.follow.db.edit_session(q.from_user.id, q.message.chat.id, follow_id)
                await acknowledge(q)
                if token:
                    session = self.follow.db.db.execute(
                        "SELECT * FROM follow_sessions WHERE id=?", (token,)).fetchone()
                    body, markup = settings_panel(session)
                    panel = await self.bot.send_message(q.message.chat.id, body, reply_markup=markup)
                    self.follow.db.bind(token, panel.message_id)
                return
            if action == "check":
                if not self.store.rate(q.from_user.id, "follow_check", 2):
                    await acknowledge(q, "Check limit reached")
                    return
                self.follow.db.db.execute("UPDATE follows SET next_check=? WHERE id=? AND user_id=?",
                                          (0, follow_id, q.from_user.id))
                await acknowledge(q, "Queued for the next monitor check")
                return
            if action == "remove":
                self.follow.db.remove(follow_id, q.from_user.id)
                await acknowledge(q)
                await self.show_following(q.message.chat.id, q.from_user.id,
                                          q.message.chat.type, q.message.message_id)
                return
            await acknowledge(q)
            return
        if data.startswith("fu:"):
            await acknowledge(q)
            if self.follow.db.remove(data[3:], q.from_user.id):
                await q.message.edit_text("Follow removed. Use /following to view the rest.")
            return
        if data.startswith("fn:"):
            parts = data.split(":")
            if len(parts) != 3 or parts[2] not in {"profile", "fetch", "thanks"}:
                await acknowledge(q)
                return
            notice = self.follow.db.notification(parts[1], q.from_user.id,
                                                 q.message.chat.id, q.message.message_id)
            if not notice:
                await acknowledge(q, "This notification belongs to another user or has expired")
                return
            if parts[2] != "thanks" and notice["url"] is None:
                await acknowledge(q, "This follow has been removed")
                return
            await acknowledge(q)
            if parts[2] == "profile":
                target = Target(notice["platform"], notice["handle"], notice["url"])
                await self.show_profile(q.message.chat.id, q.from_user.id, target)
            elif parts[2] == "fetch" and notice["kind"] == "avatar" and self.store.rate(
                    q.from_user.id, "profile", 8):
                target = Target(notice["platform"], notice["handle"], notice["url"])
                jid = self.store.create_job(q.from_user.id, q.message.chat.id, target.url)
                prompt = await self.bot.send_message(q.message.chat.id, "⏳ Checking public profile photo…")
                self.store.bind_progress(jid, prompt.message_id)
            elif parts[2] == "thanks":
                self.follow.db.db.execute("DELETE FROM follow_notifications WHERE token=?", (parts[1],))
                try:
                    await q.message.delete()
                except (TelegramBadRequest, TelegramForbiddenError):
                    await q.message.edit_reply_markup(reply_markup=None)
            return
        if data.startswith("fd:"):
            await acknowledge(q)
            parts = data.split(":")
            if len(parts) != 3:
                return
            entry = self.follow.db.db.execute(
                "SELECT d.url FROM follow_downloads d JOIN follows f ON f.id=d.follow_id "
                "WHERE d.token=? AND d.follow_id=? AND d.user_id=? AND f.user_id=? "
                "AND f.chat_id=? AND d.expires>?",
                (parts[2], parts[1], q.from_user.id, q.from_user.id, q.message.chat.id,
                 time.time())).fetchone()
            if entry and self.store.rate(q.from_user.id, "links", 12):
                jid = self.store.create_job(q.from_user.id, q.message.chat.id, entry["url"])
                await self.bot.send_message(q.message.chat.id, "⏳ Preparing your download…",
                    reply_markup=kb([[("Cancel ✕", f"j:{jid}:cancel")]]))
            return
        if data.startswith("m:"):
            await acknowledge(q)
            return
        if data.startswith("a:"):
            await acknowledge(q)
            parts = data.split(":")
            if len(parts) != 3 or q.from_user.id != self.settings.owner_telegram_id:
                return
            if parts[2] == "yes":
                access_pending = self.store.db.execute("SELECT action,target_id FROM admin_pending "
                    "WHERE id=? AND owner_id=? AND chat_id=? AND message_id=?",
                    (parts[1], q.from_user.id, q.message.chat.id, q.message.message_id)).fetchone()
                result = self.store.apply_access(
                    parts[1],
                    q.from_user.id,
                    self.settings.owner_telegram_id,
                    q.message.chat.id,
                    q.message.message_id,
                )
                if access_pending and "complete" in result and access_pending["action"] in {"remove", "group_remove"}:
                    for revocation_jid, active_task in list(self.worker.tasks.items()):
                        job = self.store.job(revocation_jid)
                        if job and (job["user_id"] if access_pending["action"] == "remove"
                                    else job["chat_id"]) == access_pending["target_id"]:
                            active_task.cancel()
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
            if len(parts) != 3 or parts[2] not in {"y", "n", "p", "q", "480", "1080", "audio"}:
                await acknowledge(q)
                return
            if parts[2] == "q":
                row = self.store.db.execute(
                    "SELECT url FROM confirmations WHERE id=? AND user_id=? AND chat_id=? "
                    "AND prompt_id=? AND state='PENDING' AND expires>?",
                    (parts[1], q.from_user.id, q.message.chat.id,
                     q.message.message_id, time.time())).fetchone()
                await acknowledge(q, "Request expired" if not row else None)
                if row and youtube_video_url(row["url"]):
                    await q.message.edit_text(
                        "🎛 <b>YouTube quality</b>\nChoose video quality or MP3.",
                        reply_markup=kb([[("🎬 Best available · up to 1080p", f"c:{parts[1]}:1080")],
                                         [("🎬 480p", f"c:{parts[1]}:480"),
                                          ("🎵 MP3", f"c:{parts[1]}:audio")],
                                         [("✖ Cancel", f"c:{parts[1]}:n")]]))
                return
            if parts[2] == "p":
                row = self.store.db.execute(
                    "SELECT url FROM confirmations WHERE id=? AND user_id=? AND chat_id=? "
                    "AND prompt_id=? AND state='PENDING' AND expires>?",
                    (parts[1], q.from_user.id, q.message.chat.id,
                     q.message.message_id, time.time())).fetchone()
                await acknowledge(q, "Request expired" if not row else None)
                if not row or not self.store.rate(q.from_user.id, "profile", 8):
                    return
                url = str(row["url"])
                try:
                    if source(url) == "tiktok":
                        url = await self.worker._resolve_short(url)
                    target = await resolve_creator_from_post(url)
                except (ValueError, TimeoutError, httpx.HTTPError):
                    target = None
                if target:
                    await self.show_profile(q.message.chat.id, q.from_user.id, target)
                else:
                    await self.bot.send_message(q.message.chat.id,
                        "Creator profile could not be identified from this public post.")
                return
            mode = parts[2] if parts[2] in {"480", "1080", "audio"} else "auto"
            state, jid = self.store.decision(
                parts[1], q.from_user.id, q.message.chat.id, q.message.message_id,
                parts[2] != "n", mode,
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
                    "⏳ Preparing your download…",
                    reply_markup=kb([[("Cancel ✕", f"j:{jid}:cancel")]]),
                )
            elif state == "EXPIRED":
                await q.message.edit_reply_markup(reply_markup=None)
            return
        if data.startswith("v:"):
            await acknowledge(q)
            parts = data.split(":")
            if len(parts) not in {4, 5}:
                return
            cid, action, arg = parts[1:4]
            row = self.store.collection(cid, q.from_user.id, q.message.chat.id)
            if not row or row["message_id"] != q.message.message_id:
                return
            items = json.loads(row["items"])
            if action == "close":
                await q.message.edit_text("Collection closed.")
                return
            if action == "back":
                context = self.store.collection_context(cid)
                sid = str(context.get("search_sid") or "")
                search_row = self.store.search(sid, q.from_user.id, q.message.chat.id)
                if search_row and search_row["generation"] == context.get("search_generation"):
                    old_message = search_row["message_id"]
                    self.store.set_search_message(sid, q.message.message_id)
                    if old_message and old_message != q.message.message_id:
                        try:
                            await self.bot.edit_message_reply_markup(
                                chat_id=q.message.chat.id, message_id=old_message,
                                reply_markup=None)
                        except TelegramBadRequest:
                            pass
                    await self.render_search(sid, q.from_user.id, q.message.chat.id,
                                             detail=int(context["search_index"]))
                else:
                    await q.message.edit_text("Search expired. Send /podcast to search again.")
                return
            if action == "follow":
                target = profile_target(str(self.store.collection_context(cid).get("source_url") or ""))
                if target:
                    await self.show_follow(q.message.chat.id, q.from_user.id, target)
            elif action == "p" and arg.isdigit():
                number = int(arg)
                await self.render_collection(cid, q.from_user.id, q.message.chat.id, number)
            elif action == "q" and (arg == "all" or arg.isdigit() and int(arg) < len(items)):
                context = self.store.collection_context(cid)
                provider = str(context.get("kind") or "")
                target = items[int(arg)] if arg.isdigit() else items[0]
                is_video = provider == "youtube" or target.get("kind") == "video"
                quality_choices = (
                    [("🎬 Best · up to 1080p", "1080"),
                     ("🎬 480p", "480"), ("🎵 MP3", "audio")]
                    if is_video else
                    [("🎵 MP3", "audio")] if target.get("kind") in {"track", "episode"}
                    else [("⬇️ Download", "auto")]
                )
                markup = kb([
                    [(label, f"v:{cid}:s:{arg}:{mode}") for label, mode in quality_choices],
                    [("◀ Back", f"v:{cid}:p:{int(arg) // 10 if arg.isdigit() else 0}"),
                     ("✖ Close", f"v:{cid}:close:0")],
                ])
                label = "All items" if arg == "all" else html.escape(str(target.get("title") or ""))
                await q.message.edit_text(
                    f"🎛 <b>{label}</b>\nChoose download quality.", reply_markup=markup
                )
            elif action == "s" and len(parts) == 5:
                mode = parts[4]
                if mode not in {"auto", "audio", "480", "720", "1080"}:
                    return
                indexes = range(len(items)) if arg == "all" else ([int(arg)] if arg.isdigit() else [])
                created_jobs = []
                for number in indexes:
                    if not 0 <= number < len(items):
                        continue
                    jid, created = self.store.select_collection(
                        cid, q.from_user.id, q.message.chat.id, number, mode
                    )
                    if created and jid:
                        created_jobs.append(jid)
                if created_jobs:
                    await self.bot.send_message(
                        q.message.chat.id,
                        f"⏳ {len(created_jobs)} items added to the download queue.",
                    )
                await self.render_collection(
                    cid, q.from_user.id, q.message.chat.id,
                    int(arg) // 10 if arg.isdigit() else 0,
                )
            return
        if data.startswith("j:"):
            await acknowledge(q)
            parts = data.split(":")
            if len(parts) == 3 and parts[2] in {"follow", "profile"}:
                job = self.store.job(parts[1])
                if not job or job["user_id"] != q.from_user.id or job["chat_id"] != q.message.chat.id:
                    return
                context = self.store.job_context(parts[1])
                url = str(context.get("source_url") or job["url"])
                target = profile_target(str(context.get("profile_url") or "")) or creator_from_post(
                    url, context)
                if not target and self.store.rate(q.from_user.id, "profile", 8):
                    try:
                        if source(url) == "tiktok":
                            url = await self.worker._resolve_short(url)
                        target = await resolve_creator_from_post(url, context)
                    except (ValueError, TimeoutError, httpx.HTTPError):
                        target = None
                if target:
                    if parts[2] == "profile":
                        await self.show_profile(q.message.chat.id, q.from_user.id, target)
                    else:
                        await self.show_follow(q.message.chat.id, q.from_user.id, target)
                else:
                    await self.bot.send_message(q.message.chat.id, "Creator profile is unavailable for this item.")
                return
            if len(parts) == 3 and parts[2] in {"avatar", "latest", "stories"}:
                job = self.store.job(parts[1])
                if not job or job["user_id"] != q.from_user.id or job["chat_id"] != q.message.chat.id:
                    return
                context = self.store.job_context(parts[1])
                if context.get("kind") != "profile" or not self.store.rate(q.from_user.id, "links", 12):
                    return
                profile_url = str(context.get("profile_url") or "")
                if parts[2] == "avatar":
                    target = clean_url(str(context.get("avatar_url") or ""))
                    mode = "document"
                    item_context = {
                        "kind": "profile_avatar", "source_url": profile_url,
                        "title": context.get("title"), "artist": context.get("artist"),
                    }
                elif parts[2] == "latest":
                    try:
                        items, _ = await self.worker._browse_profile_gallery(profile_url)
                        target = next(
                            str(x["url"]) for x in items
                            if "/video/" in x["url"] or "/photo/" in x["url"]
                            or "/p/" in x["url"] or "/status/" in x["url"]
                        )
                    except (ValueError, TimeoutError, StopIteration):
                        await self.bot.send_message(
                            q.message.chat.id, "The latest public post is unavailable."
                        )
                        return
                    mode = "auto"
                    item_context = {"source_url": target, "kind": "post"}
                else:
                    username = str(context.get("artist") or "")
                    story_target = profile_target(profile_url)
                    if not story_target or story_target.handle != username:
                        return
                    await self.show_stories(q.message.chat.id, q.from_user.id, story_target)
                    return
                jid = self.store.create_job(
                    q.from_user.id, q.message.chat.id, target, mode, item_context
                )
                status = await self.bot.send_message(
                    q.message.chat.id, "⏳ Checking public media…",
                    reply_markup=kb([[("Cancel ✕", f"j:{jid}:cancel")]]),
                )
                self.store.bind_progress(jid, status.message_id)
                return
            if len(parts) == 3 and parts[2] in {"info", "lyrics", "transcribe"}:
                job = self.store.job(parts[1])
                if not job or job["user_id"] != q.from_user.id or job["chat_id"] != q.message.chat.id:
                    return
                context = self.store.job_context(parts[1])
                if parts[2] == "transcribe":
                    await self.bot.send_message(q.message.chat.id, "📄 Episode transcription: coming soon")
                elif parts[2] == "info":
                    details = str(context.get("description") or "").strip()
                    if details:
                        for offset in range(0, min(len(details), 12000), 3500):
                            await self.bot.send_message(
                                q.message.chat.id,
                                ("ℹ️ <b>Details</b>\n" if offset == 0 else "")
                                + html.escape(details[offset : offset + 3500]),
                            )
                else:
                    try:
                        lyric = await verified_lyrics(
                            str(context.get("title") or ""),
                            str(context.get("artist") or ""),
                            int(context.get("duration") or 0) or None,
                        )
                    except Exception:
                        lyric = None
                    if lyric:
                        for offset in range(0, min(len(lyric), 12000), 3500):
                            await self.bot.send_message(
                                q.message.chat.id,
                                ("📝 <b>Lyrics</b>\n" if offset == 0 else "")
                                + html.escape(lyric[offset : offset + 3500]),
                            )
                    else:
                        await self.bot.send_message(
                            q.message.chat.id, "Complete lyrics for this song were unavailable."
                        )
                return
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
                            q.message.chat.id, "🎵 Preparing MP3…"
                        )
                        self.store.bind_progress(jid, status.message_id)
                    else:
                        await q.message.edit_text("🎵 Preparing MP3…")
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
        elif action == "quick" and len(parts) == 4 and parts[3] in {"podcast", "episode"}:
            chosen = {parts[3]} if kinds != {parts[3]} else {"podcast", "episode"}
            self.store.update_search(sid, chosen, sources)
            await self.refresh_search(sid, q.from_user.id, q.message.chat.id)
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
                lyric = await verified_lyrics(
                    str(hit["title"]), str(hit["creator"]),
                    int(hit.get("duration") or 0) or None,
                )
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
                    q.message.chat.id, "Complete lyrics for this version were unavailable."
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
            cid = self.store.create_collection(
                q.from_user.id, q.message.chat.id, title, items,
                {"source_url": str(hit["url"]), "kind": str(hit["source"]),
                 "owner_url": str(items[0].get("owner_url") or ""),
                 "search_sid": sid, "search_index": int(parts[3]),
                 "search_generation": row["generation"]},
            )
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
                    f"Mikachan · Confirm public media request\n<code>{html.escape(url[:300])}</code>",
                    reply_markup=download_prompt_markup(cid, url),
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
        identity = await self.bot.get_me()
        self.bot_username = identity.username or ""
        await self.bot.set_my_commands(
            [
                BotCommand(command=x, description=y)
                for x, y in (
                    ("help", "How to use"),
                    ("search", "Search music and podcasts"),
                    ("music", "Search music"),
                    ("podcast", "Search podcasts"),
                    ("twitter", "Twitter link help"),
                    ("instagram", "Instagram link help"),
                    ("tiktok", "TikTok link help"),
                    ("youtube", "YouTube link help"),
                    ("follow", "Follow a public profile"),
                    ("following", "Manage followed profiles"),
                    ("settings", "Preferences"),
                    ("queue", "My jobs"),
                    ("status", "Status"),
                    ("cancel", "Cancel my job"),
                    ("fal", "Reviewed Hafez fortune"),
                    ("poem", "Poetry menu"),
                    ("dice", "Roll a Telegram die"),
                    ("rules", "Group rules"),
                    ("note", "Group note"),
                    ("qr", "Create a QR image"),
                    ("scan", "Scan a replied QR or barcode"),
                    ("ocr", "Read text in a replied image"),
                    ("weather", "Weather and forecast"),
                    ("rate", "Fiat or crypto reference rate"),
                    ("now", "Local time and calendars"),
                    ("apod", "NASA astronomy picture"),
                    ("translate", "Translate selected text"),
                )
            ]
        )
        task = asyncio.create_task(self.worker.loop())
        follow_task = asyncio.create_task(self.follow.loop())
        try:
            await self.dp.start_polling(self.bot)
        finally:
            self.follow.running = False
            follow_task.cancel()
            await asyncio.gather(follow_task, return_exceptions=True)
            await self.worker.stop()
            task.cancel()
            await self.bot.session.close()
