"""Native Telegram interface. All actions recheck actor, chat and session ownership."""

import asyncio
import html
import json
import os
import re
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
from .utilities import Utilities, _market_card, qr_png
from .userbot import UserbotBridge, is_currency_query, send_currency_reply
from .proxy_relay import ProxyRelay
from .mikasub_core import MikaSubCore
from .mikasub_ui import MikaSubUI
from .instagram_relay import InstagramRelay
from .external_utilities import ExternalUtilities
from .explore import Explore
from .content_ui import ContentUI, COMMANDS as CONTENT_COMMANDS
from .n8n_gateway import GatewayError, N8NGateway
from .intelligence import Intelligence
from .audio_ui import AudioUI
from .local_sources import LocalSources
from .market_prices import parse_asset_query
from .personality import Mode, mode_for, response
from .public_social import public_x_media, public_x_post
from .settings_ui import SettingsUI
from .groupstatus import GroupStatus, alias as group_stats_alias
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
    quality_choice = youtube_video_url(url) or source(url) == "pornhub"
    rows = [[("✅ Yes", f"c:{cid}:{'q' if quality_choice else 'y'}"),
             ("❌ No", f"c:{cid}:n")]]
    if source(url) in SOCIAL_PLATFORMS and profile_target(url) is None:
        rows.append([("👤 Profile", f"c:{cid}:p")])
    return kb(rows)


def collection_view(
    cid: str, title: str, items: Sequence[Mapping[str, object]], page: int,
    context: dict[str, object], batch: tuple[int, int, bool] = (0, 0, False),
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
    if batch[2]:
        rows.append([("⏹ Remaining downloads stopped", f"v:{cid}:status:0")])
    elif batch[0] == len(items) and not batch[1]:
        rows.append([("✅ Selected items processed", f"v:{cid}:status:0")])
    else:
        rows.append([("⬇️ Download all", f"v:{cid}:q:all")])
        if batch[1]:
            rows.append([("⏹ Stop remaining downloads", f"v:{cid}:stop:0")])
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
    if batch[0]:
        text += f"\n⬇️ {batch[0]} selected · {batch[1]} remaining"
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
    "✦ <b>MIKACHAN</b>\n"
    "Your private toolkit. Choose a task below.\n\n"
    "⬇️ <b>DOWNLOAD &amp; DISCOVER</b>\n"
    "Send a supported link → choose <b>Yes</b> or <b>No</b>.\n"
    "/search QUERY · find media\n"
    "/music QUERY · songs  |  /podcast QUERY · shows\n"
    "/book TITLE · local books  |  /queue · download status\n\n"
    "👀 <b>PROFILES &amp; FOLLOWING</b>\n"
    "/instagram USER · /twitter USER · /tiktok USER · /youtube USER\n"
    "/follow PROFILE_URL · watch a profile\n"
    "/explore #TOPIC · discover TikTok posts\n"
    "/following · manage and check follows\n\n"
    "🛠 <b>QUICK TOOLS</b>\n"
    "/weather CITY · forecast  |  /now · local time\n"
    "/nasa · astronomy picture  |  /qr TEXT · QR code\n"
    "Reply to a file or image: /url · /scan · /ocr · /st · /xst\n"
    "/tr fa TEXT · translate  |  /asr · transcribe replied audio\n\n"
    "🎭 <b>CULTURE &amp; MORE</b>\n"
    "/fal · Hafez  |  /poem · poetry  |  /dice · roll\n"
    "/pishi · cats  |  /earth · nature  |  /art · art\n"
    "/proxy · proxies  |  /vpn · subscriptions\n"
    "Send <b>دلار</b>, <b>سکه</b> or <b>طلا</b> for Toman quotes.\n"
)


def help_text(*, relay_enabled: bool, group: bool, closing: str) -> str:
    sections = [HELP_TEXT]
    if relay_enabled:
        sections.append(
            "\n🗣 <b>MIKACHAN AI</b>\n"
            "/mika QUESTION or <b>میکا QUESTION</b>\n"
            "Reply to an AI answer to continue the conversation.\n"
            "/tts TEXT · speech  |  /stt · reply to audio\n"
        )
    if group:
        sections.append(
            "\n👥 <b>IN GROUPS</b>\n"
            "/rules · /notes · /groupstats\n"
            "Reply to a member with /id for their profile.\n"
            "Admins: /groupsettings · /setnote · /mute · /del\n"
        )
    sections.append("\n⚙️ /settings · preferences  |  /status · bot status\n"
                    "<i>" + html.escape(closing) + "</i>")
    return "".join(sections)


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.data_dir / "mikachan.sqlite")
        self.store.recover()
        self._clear_interrupted_media()
        self.store.seed_access(settings.allowlist)
        self.store.seed_groups(settings.groups)
        self.store.bootstrap_access(settings.owner_telegram_id)
        self.searcher = Searcher()
        # Public Bot API uploads can take longer than aiogram's 60-second default.
        session = AiohttpSession(timeout=300)
        if settings.bot_api_mode == "local":
            assert settings.local_bot_api_url is not None
            session = AiohttpSession(
                api=TelegramAPIServer.from_base(settings.local_bot_api_url, is_local=True),
                timeout=300,
            )
        self.bot = Bot(
            settings.bot_token, session=session, default=DefaultBotProperties(parse_mode="HTML")
        )
        user_session = settings.userbot_session or settings.coinpj_session
        self.userbot = (UserbotBridge(settings.telegram_api_id, settings.telegram_api_hash,
                                      user_session, int(settings.userbot_staging_chat_id or 0))
                        if user_session and settings.telegram_api_id and
                        settings.telegram_api_hash else None)
        self.proxy_relay = ProxyRelay(self.userbot) if self.userbot else None
        self.worker = Worker(settings, self.store, self.bot)
        self.worker.userbot = self.userbot if settings.userbot_staging_chat_id else None
        self.instagram_relay = (InstagramRelay(self.userbot, self.bot, settings,
                                              self.worker, self.show_follow)
                                if self.userbot else None)
        self.worker.instagram_relay = self.instagram_relay
        self.store.db.execute("CREATE TABLE IF NOT EXISTS url_links ("
                              "token TEXT PRIMARY KEY,user_id INTEGER NOT NULL,"
                              "chat_id INTEGER NOT NULL,message_id INTEGER,"
                              "url TEXT NOT NULL,expires REAL NOT NULL)")
        self.follow = FollowService(self.store, self.bot, settings.private_chats_only)
        self._check_all_tasks: dict[int, asyncio.Task[None]] = {}
        self._quality_tasks: set[asyncio.Task[None]] = set()
        community_store = CommunityStore(self.store.db)
        community_store.seed_hafez(Path(__file__).parent / "content/hafez.json")
        self.community = Community(self.bot, community_store, settings.owner_telegram_id,
                                   self.store.access_registry)
        self.utilities = Utilities(self.bot, settings, self.store.db)
        self.external_utilities = ExternalUtilities(self.userbot, self.bot)
        self.explore = Explore(self.bot)
        self.content_ui = ContentUI(self.store.db, self.bot,
                                    enabled=settings.mika_content_enabled,
                                    authorize=lambda user, chat: self.authorized(
                                        user, chat, "private" if user == chat else "supergroup"))
        self.n8n_gateway = N8NGateway(enabled=settings.mika_n8n_enabled,
            base_url=settings.mika_n8n_base_url,
            webhook_path=settings.mika_n8n_webhook_path,
            auth_ref=settings.mika_n8n_auth_ref,
            timeout=settings.mika_n8n_webhook_timeout_s,
            max_parallel=settings.mika_ai_global_concurrency)
        self.audio_ui = AudioUI(self.store.db, self.bot, self.n8n_gateway,
                                tts_enabled=settings.mika_tts_enabled,
                                stt_enabled=settings.mika_stt_enabled,
                                poll_seconds=settings.mika_n8n_job_status_poll_s,
                                authorize=lambda user, chat: self.authorized(
                                    user, chat, "private" if user == chat else "supergroup"))
        self.local_sources = LocalSources(self.store.db, self.userbot, self.bot,
                                          settings.data_dir)
        assert self.store.access_registry is not None
        self.mikasub_core = MikaSubCore(settings, self.store.db)
        self.mikasub = MikaSubUI(self.mikasub_core, self.store, self.bot,
                                self.store.access_registry)
        self.settings_ui = SettingsUI(self.store.db, self.store.access_registry,
                                      self.utilities.now, self.bot, self.local_sources,
                                      self.mikasub, self.content_ui, self.n8n_gateway,
                                      self.audio_ui)
        self.group_status = GroupStatus(self.store.db, self.store.access_registry)
        self.intelligence = Intelligence(self.store.db, self.store.access_registry)
        self.settings_ui.intelligence = self.intelligence
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
        if self.store.access_registry is not None:
            return self.store.can_access(user_id, chat_id, chat_type)
        return allowed(
            user_id, chat_id, chat_type, self.store.allowlist(), self.settings.private_chats_only,
            self.store.groups()
        )

    def module_enabled(self, domain: str, chat_id: int | None = None) -> bool:
        access = self.store.access_registry
        if access is not None and not access.module_enabled(domain):
            return False
        if chat_id is not None and chat_id < 0 and hasattr(self, "community"):
            return self.community.store.enabled(chat_id, domain)
        return True

    async def relay_n8n_command(self, m: Message, text: str) -> None:
        """Send only an approved command and its needed reply data to n8n."""
        if not m.from_user or not self.authorized(m.from_user.id, m.chat.id, m.chat.type):
            return
        if not self.store.rate(m.from_user.id, "n8n_relay", 6):
            await m.reply("Too many audio or AI requests. Try again shortly.")
            return
        reply = getattr(m, "reply_to_message", None)
        source: dict[str, object] | None = None
        if reply:
            source = {"message_id": reply.message_id,
                      "text": (reply.text or "")[:3000],
                      "caption": (reply.caption or "")[:1000],
                      "from": {"id": reply.from_user.id,
                               "first_name": reply.from_user.first_name[:80]}
                      if reply.from_user else {}}
            for kind in ("voice", "audio"):
                media = getattr(reply, kind, None)
                if media:
                    source[kind] = {"file_id": media.file_id,
                                    "file_size": media.file_size or 0}
        message: dict[str, object] = {
            "message_id": m.message_id, "text": text[:3000],
            "chat": {"id": m.chat.id, "type": m.chat.type},
            "from": {"id": m.from_user.id},
            "verified_daniel": bool(m.from_user.id == self.settings.owner_telegram_id),
            "adult_verified": False,
            "mommy_enabled": False,
            "mika_mode": "mommy" if m.from_user.id in getattr(
                getattr(self, "settings", None), "mommy_ids", set()) else "normal",
        }
        if source:
            message["reply_to_message"] = source
            if (reply and reply.from_user and reply.from_user.id == self.bot.id and
                    not text.startswith("/") and not text.startswith("میکا")):
                message["relay_reply"] = True
        if getattr(m, "message_thread_id", None):
            message["message_thread_id"] = m.message_thread_id
        try:
            await self.n8n_gateway.relay_telegram(message)
        except GatewayError as exc:
            await m.reply("⚠️ " + html.escape(str(exc)))

    async def message(self, m: Message) -> None:
        if hasattr(self, "group_status"):
            self.group_status.observe(m)
        if hasattr(self, "intelligence"):
            self.intelligence.observe(m)
        if (getattr(m, "new_chat_members", None) and m.chat.id in self.store.groups()
                and self.module_enabled("community", m.chat.id)):
            await self.community.welcome(m, self.store.allowlist())
        if not m.from_user or not self.authorized(m.from_user.id, m.chat.id, m.chat.type):
            return
        if hasattr(self, "group_status") and await self.group_status.reply(m):
            return
        if (self.module_enabled("utilities", m.chat.id) and hasattr(self, "audio_ui")
                and await self.audio_ui.reply(m)):
            return
        if (m.chat.type in {"group", "supergroup"} and
                await self.community.content.pending_message(m)):
            return
        if not m.text:
            # A photo or image file can also carry the conversion command as its caption.
            caption = " ".join((m.caption or "").replace("ي", "ی").replace("ك", "ک").split())
            if caption in {"/st", "عکس به استیکر"} and self.module_enabled("utilities", m.chat.id):
                await self.utilities.command(m, "/st", "")
            return
        text = m.text.strip()
        normalized_text = " ".join(text.replace("ي", "ی").replace("ك", "ک").split())
        if ((normalized_text in {"آیدی", "ایدی"} or
             normalized_text.casefold() == "id" and m.reply_to_message) and
                m.chat.type in {"group", "supergroup"}):
            if self.module_enabled("community", m.chat.id):
                await self.group_status.show_identity(m)
            return
        if normalized_text == "لینک" and m.reply_to_message:
            if self.module_enabled("utilities", m.chat.id):
                await self.command(m, "/url")
            return
        if normalized_text == "پروکسی":
            if self.module_enabled("utilities", m.chat.id):
                await self.command(m, "/proxy")
            return
        if normalized_text in {"عکس به استیکر", "استیکر به عکس"}:
            if not self.module_enabled("utilities", m.chat.id):
                return
            await self.utilities.command(m, "/st" if normalized_text == "عکس به استیکر"
                                         else "/xst", "")
            return
        if hasattr(self, "mikasub") and await self.mikasub.reply(m):
            return
        if hasattr(self, "settings_ui") and await self.settings_ui.reply(m):
            return
        if hasattr(self, "intelligence") and await self.intelligence.reply(m):
            return
        if (self.module_enabled("content", m.chat.id) and hasattr(self, "content_ui")
                and await self.content_ui.reply(m)):
            return
        reply = getattr(m, "reply_to_message", None)
        relay_reply = bool(reply and reply.from_user and
                           reply.from_user.id == self.bot.id and
                           getattr(self.settings, "mika_n8n_telegram_relay", False))
        if (relay_reply and m.text and m.text.strip() and
                not m.text.lstrip().startswith("/") and
                self.module_enabled("utilities", m.chat.id)):
            await self.relay_n8n_command(m, m.text.strip())
            return
        if (m.chat.type in {"group", "supergroup"} and
                self.module_enabled("community", m.chat.id) and
                await self.community.content.keyword(m, normalized_text)):
            return
        stats_kind = group_stats_alias(text) if m.chat.type in {"group", "supergroup"} else None
        if stats_kind and hasattr(self, "group_status"):
            if not self.module_enabled("community", m.chat.id):
                return
            await self.group_status.show(m, stats_kind)
            return
        if (text in {"فال", "شعر"} and not self.module_enabled("community", m.chat.id) or
                (text in {"ارز", "هواشناسی", "اکنون", "امروز"} or
                 parse_asset_query(text) is not None or
                 text.startswith(("ارز ", "هواشناسی ", "اکنون ", "امروز "))) and
                not self.module_enabled("utilities", m.chat.id)):
            return
        if text.startswith("/") and "@" in text.split(" ", 1)[0]:
            target_bot = text.split(" ", 1)[0].split("@", 1)[1]
            if getattr(self, "bot_username", "") and target_bot.lower() != self.bot_username.lower():
                return
        if m.chat.type in {"group", "supergroup"}:
            command_name = text.split(" ", 1)[0].split("@")[0].lower().lstrip("/")
            explicit_text = (text in {"فال", "شعر", "ارز"} or parse_asset_query(text) is not None or
                             text.startswith(("ارز ", "هواشناسی ", "اکنون ", "امروز ")) or
                             text in {"هواشناسی", "اکنون", "امروز"})
            builtin = command_name in {
                "help", "search", "music", "podcast", "pod", "book", "id", "twitter", "instagram",
                "follow", "following", "explore",
                "tiktok", "youtube", "settings", "queue", "status",
                "fal", "poem", "quote", "pishi", "phishi", "earth", "art", "xotd", "wiki",
                "dice", "nickname", "rules", "setrules",
                "notes", "setnote", "delrules", "welcome", "del",
                "mute", "modlog", "groupstats", "groupstatus", "groupsettings", "qr", "scan", "ocr",
                "st", "xst", "weather", "now", "nasa", "tr", "pron", "asr", "style", "mika",
                "tts", "stt",
                "url", "link", "vpn", "proxy"}
            if explicit_text or text.startswith("/") and builtin:
                self.community.store.use(m.chat.id, m.from_user.id, "request")
        if (m.chat.type in {"group", "supergroup"} and
                not self.community.store.enabled(m.chat.id, "utilities") and
                (text == "ارز" or parse_asset_query(text) is not None or
                 text.startswith(("ارز ", "هواشناسی ", "اکنون ", "امروز ")) or
                 text in {"هواشناسی", "اکنون", "امروز"})):
            return
        userbot = getattr(self, "userbot", None)
        if userbot and (text == "ارز" or is_currency_query(text)) and self.module_enabled(
                "utilities", m.chat.id):
            if not self.store.rate(m.from_user.id, "currency", 8):
                return
            try:
                await send_currency_reply(m, text, await userbot.currency_message(text))
            except ValueError as exc:
                await m.reply(html.escape(str(exc)))
            return
        if text in {"اکنون", "امروز"} or text.startswith(("اکنون ", "امروز ")):
            await self.utilities.command(m, "/now", text.split(" ", 1)[1].strip() if " " in text else "")
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
            await self.utilities.asset_quote(m, f"{symbol} IRT" if amount == 1 else
                                             f"{amount} {symbol} IRT")
            return
        if text.startswith("ارز "):
            await self.utilities.asset_quote(m, text[4:].strip())
            return
        if text.startswith("/"):
            self.store.clear_query(m.from_user.id, m.chat.id)
            await self.command(m, text)
            return
        if (self.settings.mika_n8n_telegram_relay and
                self.module_enabled("utilities", m.chat.id) and
                (text == "میکا" or text.startswith("میکا ") or
                 re.match(r"^mika(?:\s|$)", text, re.I) or
                 text.casefold() == "can i see you")):
            await self.relay_n8n_command(m, text)
            return
        if hasattr(self, "community") and await self.community.search_reply(m):
            return
        if hasattr(self, "utilities") and hasattr(self.utilities, "now") and \
                await self.utilities.now.reply(m):
            return
        urls, extra = links(text, self.settings.max_links_per_message)
        if urls:
            if not self.module_enabled("downloader", m.chat.id):
                return
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
            if mode == "book":
                try:
                    await self.local_sources.show_books(m.chat.id, m.from_user.id, text)
                except ValueError as exc:
                    await m.reply(html.escape(str(exc)))
            else:
                await self.begin_search(m, text, mode)

    async def command(self, m: Message, raw: str) -> None:
        if m.from_user is None:
            return
        word, _, arg = raw.partition(" ")
        cmd = word.split("@")[0].lower()
        if cmd == "/link":
            cmd = "/url"
        arg = arg.strip()
        downloader_commands = {"/search", "/music", "/podcast", "/pod", "/book"}
        follow_commands = {"/follow", "/following", "/explore", "/twitter", "/instagram",
                           "/tiktok", "/youtube"}
        utility_commands = {"/qr", "/scan", "/ocr", "/tr", "/pron", "/asr", "/style", "/mika",
                            "/tts", "/stt",
                            "/st", "/xst", "/weather", "/now",
                            "/nasa", "/url", "/proxy", "/vpn", "/image", "/sticker", "/stickerimage",
                            "/gif", "/pack", "/convert"}
        if (cmd in CONTENT_COMMANDS and not self.module_enabled("content", m.chat.id) or
                cmd in downloader_commands and not self.module_enabled("downloader", m.chat.id) or
                cmd in follow_commands and not self.module_enabled("follow", m.chat.id) or
                cmd in utility_commands and not self.module_enabled("utilities", m.chat.id) or
                cmd not in downloader_commands | follow_commands | utility_commands | CONTENT_COMMANDS |
                    {"/help", "/settings", "/queue", "/status"} and
                    not self.module_enabled("community", m.chat.id)):
            return
        if cmd == "/rate":
            return  # Removed public command; currency phrases still use their existing provider.
        if cmd in CONTENT_COMMANDS and not self.store.rate(m.from_user.id, "content", 8):
            await m.reply("Content requests are limited to 8 per minute. Try again shortly.")
            return
        if hasattr(self, "content_ui") and await self.content_ui.command(m, cmd, arg):
            return
        if (getattr(getattr(self, "settings", None), "mika_n8n_telegram_relay", False)
                and cmd in {"/tts", "/stt", "/mika"}):
            if cmd == "/tts" and not (arg or m.reply_to_message and
                                       (m.reply_to_message.text or m.reply_to_message.caption)):
                await m.reply("Use /tts TEXT or reply to text with /tts.")
                return
            if cmd == "/stt" and not (m.reply_to_message and
                                       (m.reply_to_message.voice or m.reply_to_message.audio)):
                await m.reply("Reply to a voice or audio message with /stt.")
                return
            if cmd == "/mika" and not (arg or m.reply_to_message):
                await m.reply("Use /mika QUESTION or reply to a message with /mika.")
                return
            await self.relay_n8n_command(m, raw)
            return
        if cmd == "/mika":
            await m.reply("Mikachan AI is not connected to the owner's n8n workflow yet.")
            return
        if cmd == "/explore":
            if self.store.rate(m.from_user.id, "explore", 5):
                await self.explore.command(m, arg)
            return
        if hasattr(self, "audio_ui") and await self.audio_ui.command(m, cmd, arg):
            return
        if hasattr(self, "external_utilities") and await self.external_utilities.command(m, cmd, arg):
            return
        if cmd == "/vpn":
            await self.mikasub.open_user(m)
            return
        if cmd == "/settings" and hasattr(self, "settings_ui"):
            await self.settings_ui.show(m)
            return
        if cmd == "/groupstatus" and hasattr(self, "group_status"):
            await self.group_status.show(m)
            return
        if cmd == "/id" and m.chat.type in {"group", "supergroup"}:
            await self.group_status.show_identity(m)
            return
        if await self.community.command(m, cmd, arg):
            return
        if (m.chat.type in {"group", "supergroup"} and
                not self.community.store.enabled(m.chat.id, "utilities") and
                cmd in {"/qr", "/scan", "/st", "/xst", "/weather", "/now",
                        "/nasa", "/url"}):
            return
        if cmd == "/url":
            if not self.userbot:
                await m.reply("File links need USERBOT_SESSION. Run the QR login command "
                              "from private SSH and save the session in server configuration.")
            elif not m.reply_to_message:
                await m.reply("Reply to a file, photo, video, or audio message with /url.")
            elif self.store.rate(m.from_user.id, "file_to_link", 5):
                try:
                    link = await self.userbot.file_to_link(self.bot, m.chat.id,
                                                           m.reply_to_message.message_id,
                                                           m.reply_to_message)
                    token = secrets.token_urlsafe(9)
                    buttons = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="▣ QR", callback_data=f"url:{token}"),
                        InlineKeyboardButton(text="⬇ Download", url=link)]])
                    sent = await m.reply("✅ <b>Your link is ready</b>\n\n🔗 " +
                                         html.escape(link) + "\n\n⏳ Expires within 24 hours.",
                                         reply_markup=buttons)
                    self.store.db.execute("INSERT INTO url_links VALUES (?,?,?,?,?,?)",
                                          (token, m.from_user.id, m.chat.id,
                                           sent.message_id, link, time.time() + 86400))
                except ValueError as exc:
                    await m.reply(html.escape(str(exc)))
            return
        if cmd == "/proxy":
            if self.proxy_relay is None:
                await m.reply("Proxy lists need a Telegram user account session. "
                              "Run the QR userbot login from the server terminal first.")
            elif self.store.rate(m.from_user.id, "proxy", 4):
                try:
                    await self.proxy_relay.open(m)
                except ValueError as exc:
                    await m.reply(html.escape(str(exc)))
            return
        if cmd == "/book":
            if arg:
                if not self.store.rate(m.from_user.id, "book", 5):
                    return
                try:
                    await self.local_sources.show_books(m.chat.id, m.from_user.id, arg)
                except ValueError as exc:
                    await m.reply(html.escape(str(exc)))
            else:
                self.store.prompt_query(m.from_user.id, m.chat.id, "book", 600)
                await m.reply("📚 Send a book title or author in the next 10 minutes.")
            return
        if await self.utilities.command(m, cmd, arg):
            return
        if cmd == "/help":
            personality_mode = (mode_for(m.from_user.id, self.store.allowlist(), self.settings.mommy_ids)
                    if m.chat.type == "private" else Mode.NORMAL)
            await m.reply(help_text(
                relay_enabled=self.settings.mika_n8n_telegram_relay,
                group=m.chat.type in {"group", "supergroup"},
                closing=response("ready", personality_mode)))
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
            active = [j for j in self.store.jobs(m.from_user.id) if j["state"] in
                      {"QUEUED", "RESOLVING", "DOWNLOADING", "VERIFYING", "DELIVERING"}]
            if not active:
                await m.reply("✅ No active downloads.")
            else:
                await m.reply("⬇️ <b>Active downloads</b>\n" + "\n".join(
                    f"{i}. {html.escape(str(j['state']).title())}"
                    for i, j in enumerate(active, 1)),
                    reply_markup=kb([[(f"✖ Cancel {i}", f"j:{j['id']}:cancel")]
                                     for i, j in enumerate(active, 1)]))
        elif cmd == "/status":
            await m.reply(
                f"Jobs: {len(self.store.jobs(m.from_user.id))} recent · Active: {len(self.worker.tasks)}"
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
        if mode in {"music", "podcast"} and hasattr(self, "local_sources") and \
                self.local_sources.has_active(mode):
            try:
                await self.local_sources.show_files(m.chat.id, m.from_user.id, query, mode)
            except ValueError as exc:
                await m.reply("⚠️ Local source search: " + html.escape(str(exc)))

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
            cid, str(row["title"]), items, page, self.store.collection_context(cid),
            self.store.collection_batch(cid, user, chat),
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
            "SELECT id,platform,handle,chat_id,failures FROM follows WHERE user_id=? "
            + ("" if chat_type == "private" else "AND chat_id=? ")
            + "ORDER BY platform,handle",
            (user,) if chat_type == "private" else (user, chat),
        ).fetchall()
        body = "👀 <b>Following</b>\nChoose a profile to manage."
        if not rows:
            body += "\nNo profiles yet. Use /follow to begin."
        buttons = [[(f"{'⚠️' if row['failures'] else '👤'} {row['platform'].title()} · @{row['handle'][:28]}",
                     f"fm:{row['id']}:open")] for row in rows]
        if rows:
            buttons.append([("🔄 Check now · all", "fm:all:check")])
        buttons.append([("✖ Close", "fm:close:close")])
        if message_id is None:
            sent = await self.bot.send_message(chat, body, reply_markup=kb(buttons))
            self.follow.db.bind_menu(user, chat, sent.message_id)
        else:
            await self.bot.edit_message_text(body, chat_id=chat, message_id=message_id,
                                             reply_markup=kb(buttons))
            self.follow.db.bind_menu(user, chat, message_id)

    async def check_all_following(self, chat: int, user: int, chat_type: str) -> None:
        rows = self.follow.db.db.execute(
            "SELECT id FROM follows WHERE user_id=? "
            + ("" if chat_type == "private" else "AND chat_id=? ")
            + "ORDER BY next_check LIMIT 25",
            (user,) if chat_type == "private" else (user, chat),
        ).fetchall()
        good = partial = unavailable = 0
        for record in rows:
            async with self.follow.check_lock:
                row = self.follow.db.db.execute(
                    "SELECT * FROM follows WHERE id=? AND user_id=?", (record["id"], user)
                ).fetchone()
                if row is None:
                    continue
                target_type = "private" if row["chat_id"] == user else "group"
                if not self.authorized(user, row["chat_id"], target_type):
                    continue
                try:
                    health = await self.follow.check(row)
                except (ValueError, TimeoutError, OSError, httpx.HTTPError):
                    unavailable += 1
                    continue
                if health and all(health.values()):
                    good += 1
                elif any(health.values()):
                    partial += 1
                else:
                    unavailable += 1
        if self.authorized(user, chat, chat_type):
            await self.bot.send_message(chat,
                f"🔄 Follow check complete\n"
                f"✅ Accessible: {good}\n"
                f"◐ Partly accessible: {partial}\n"
                f"⚠️ Unavailable: {unavailable}")

    async def show_profile(self, chat: int, user: int, target: Target) -> None:
        if target.platform == "instagram":
            relay = getattr(self, "instagram_relay", None)
            if not relay:
                await self.bot.send_message(chat, "Instagram profiles need a Telegram user "
                                            "account session. Run userbot-login on the server.")
                return
            progress = await self.bot.send_message(chat, "⏳ Finding Instagram profile…")
            try:
                await relay.show_profile(chat, user, target.handle)
            except ValueError as exc:
                await self.bot.send_message(chat, html.escape(str(exc)))
            finally:
                try:
                    await self.bot.delete_message(chat, progress.message_id)
                except Exception:
                    pass
            return
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
            if target.platform == "instagram":
                rows.append([("🎞 Latest Reel", f"p:{token}:reels")])
            rows.append([("🖼 Original profile photo", f"p:{token}:original")])
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
            if target.platform != "youtube":
                try:
                    structured = await asyncio.wait_for(
                        public_profile(target, display_avatar=True), 7)
                    details = {key: structured.get(key) or details.get(key, "")
                               for key in ("name", "bio", "avatar")}
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
        if target.platform == "instagram":
            await self.show_profile(chat, user, target)
            return
        try:
            stories = await public_items(target, "stories")
        except (ValueError, TimeoutError, OSError):
            await self.bot.send_message(
                chat, "⚠️ Public stories could not be checked. This platform may require login."
            )
            return
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
        if q.message is None or isinstance(q.message, InaccessibleMessage):
            await acknowledge(q)
            return
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
        if target.platform == "instagram" and action != "follow":
            await acknowledge(q)
            await self.show_profile(q.message.chat.id, q.from_user.id, target)
            return
        if action in {"card", "stories", "latest", "shorts", "reels", "original"} and not self.store.rate(
                q.from_user.id, "profile", 8):
            await acknowledge(q, "Please wait before checking more profiles")
            return
        await acknowledge(q)
        if action == "follow":
            await self.show_follow(q.message.chat.id, q.from_user.id, target)
        elif action == "card" and target.platform in {"instagram", "tiktok"}:
            try:
                details = await asyncio.wait_for(public_profile(target, display_avatar=True), 30)
                body = (f"👤 <b>{html.escape(details.get('name') or target.handle)}</b>\n"
                        f"@{html.escape(target.handle)} · {target.platform.title()}\n\n"
                        f"{html.escape(details.get('bio', ''))}")
                avatar_url = details.get("avatar")
                if avatar_url:
                    try:
                        photo = await profile_photo(avatar_url)
                        await self.bot.send_photo(q.message.chat.id, photo, caption=body[:1024])
                        return
                    except (ValueError, TimeoutError, OSError, httpx.HTTPError):
                        pass
                await self.bot.send_message(q.message.chat.id, body)
            except (ValueError, TimeoutError, OSError, httpx.HTTPError):
                await self.bot.send_message(q.message.chat.id,
                    "⚠️ This public profile is not accessible from the server. "
                    "Instagram may require a permitted login session.")
        elif action == "stories" and target.platform in {"instagram", "tiktok"}:
            await self.show_stories(q.message.chat.id, q.from_user.id, target)
        elif action == "original" and target.platform in {"instagram", "tiktok"}:
            try:
                details = await public_profile(target, display_avatar=True)
                url = details.get("avatar")
                if not url:
                    raise ValueError("no public original avatar")
                photo = await profile_photo(url)
                await self.bot.send_document(q.message.chat.id, photo,
                                             caption=f"👤 @{html.escape(target.handle)} · original photo")
            except (ValueError, TimeoutError, OSError, httpx.HTTPError, TelegramBadRequest):
                await self.bot.send_message(q.message.chat.id,
                                            "Original profile photo is unavailable from public sources.")
        elif action in {"latest", "shorts", "reels"}:
            try:
                items = await public_items(target, "shorts" if action == "shorts" else
                                           "reels" if action == "reels" else "posts")
            except (ValueError, TimeoutError, OSError):
                await self.bot.send_message(
                    q.message.chat.id, "Public posts could not be checked right now. Try again later.")
                return
            if not items:
                await self.bot.send_message(q.message.chat.id, "No accessible public posts found.")
                return
            item_url = items[0][1]
            if target.platform == "twitter":
                try:
                    if await public_x_media(item_url) is None:
                        await self.bot.send_message(
                            q.message.chat.id,
                            f"📝 <b>Latest post · @{html.escape(target.handle)}</b>\n\n"
                            f"{html.escape(items[0][2][:1000])}",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="🔗 Open post", url=item_url)]]))
                        return
                except (ValueError, TimeoutError, httpx.HTTPError):
                    pass
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
        if q.message is None or isinstance(q.message, InaccessibleMessage):
            await acknowledge(q)
            return
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
        if (data.startswith("ct:") and
                not self.module_enabled("content", q.message.chat.id) or
                data.startswith("au:") and
                not self.module_enabled("utilities", q.message.chat.id)):
            await acknowledge(q, "This module is currently unavailable")
            return
        if data.startswith("ct:") and data.split(":")[-1] not in {"done", "back"} and \
                not self.store.rate(q.from_user.id, "content", 15):
            await acknowledge(q, "Content requests are temporarily rate limited")
            return
        if data.startswith("url:"):
            entry = self.store.db.execute("SELECT url FROM url_links WHERE token=? AND "
                    "user_id=? AND chat_id=? AND message_id=? AND expires>?",
                    (data[4:], q.from_user.id, q.message.chat.id, q.message.message_id,
                     time.time())).fetchone()
            if entry:
                await acknowledge(q)
                await self.bot.send_photo(q.message.chat.id,
                    BufferedInputFile(qr_png(str(entry[0])), "link-qr.png"),
                    caption="▣ Scan to open your link.")
            else:
                await acknowledge(q, "Link expired or unavailable")
            return
        if hasattr(self, "group_status") and await self.group_status.callback(q, data):
            return
        if hasattr(self, "intelligence") and await self.intelligence.callback(q, data):
            return
        if hasattr(self, "settings_ui") and await self.settings_ui.callback(q, data):
            for job_id, active_task in list(self.worker.tasks.items()):
                job = self.store.job(job_id)
                if job:
                    kind = "private" if job["chat_id"] == job["user_id"] else "group"
                    if not self.authorized(job["user_id"], job["chat_id"], kind):
                        active_task.cancel()
            return
        if hasattr(self, "content_ui") and await self.content_ui.callback(q, data):
            return
        if data.startswith("ex:"):
            if not self.module_enabled("follow", q.message.chat.id):
                await acknowledge(q, "Explore is currently unavailable")
            elif self.store.rate(q.from_user.id, "explore", 5):
                await self.explore.callback(q)
            else:
                await acknowledge(q, "Explore limit reached")
            return
        if hasattr(self, "audio_ui") and await self.audio_ui.callback(q, data):
            return
        prefix = data.split(":", 1)[0]
        domain = ({"c": "downloader", "s": "downloader", "b": "downloader",
                   "lb": "downloader",
                   "f": "follow", "fm": "follow", "fu": "follow", "p": "follow",
                   "fn": "follow", "fd": "follow", "u": "community", "g": "community",
                   "gs": "community", "gc": "community", "n": "utilities",
                   "px": "utilities", "ms": "utilities", "uh": "utilities",
                   "ir": "follow", "au": "utilities", "ct": "content"}).get(prefix)
        if domain and not self.module_enabled(domain, q.message.chat.id):
            await acknowledge(q, "This module is currently unavailable")
            return
        if hasattr(self, "mikasub") and await self.mikasub.callback(q, data):
            return
        if hasattr(self, "external_utilities") and await self.external_utilities.callback(q):
            return
        if hasattr(self, "local_sources") and await self.local_sources.callback(q):
            return
        if data.startswith("px:"):
            if self.proxy_relay:
                try:
                    await self.proxy_relay.click(q)
                except ValueError as exc:
                    await self.bot.send_message(q.message.chat.id, html.escape(str(exc)))
            else:
                await acknowledge(q, "Proxy session unavailable")
            return
        if data.startswith("ir:"):
            if data.endswith(":mp3") and not self.store.rate(q.from_user.id, "instagram_mp3", 3):
                await acknowledge(q, "MP3 limit reached; try again shortly")
                return
            if self.instagram_relay:
                try:
                    await self.instagram_relay.callback(q)
                except ValueError as exc:
                    await self.bot.send_message(q.message.chat.id, html.escape(str(exc)))
            else:
                await acknowledge(q, "Instagram session unavailable")
            return
        if hasattr(self, "utilities") and hasattr(self.utilities, "now") and \
                await self.utilities.now.callback(q, data):
            return
        if (hasattr(self, "community") and
                await self.community.content.callback(q, data)):
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
            if follow_id == "all" and action == "check":
                tasks = getattr(self, "_check_all_tasks", None)
                if tasks is None:
                    tasks = self._check_all_tasks = {}
                existing = tasks.get(q.from_user.id)
                if existing and not existing.done():
                    await acknowledge(q, "A full check is already running")
                    return
                if not self.store.rate(q.from_user.id, "follow_check_all", 1):
                    await acknowledge(q, "Check limit reached")
                    return
                await acknowledge(q, "Checking followed profiles…")
                check_task = asyncio.create_task(self.check_all_following(
                    q.message.chat.id, q.from_user.id, q.message.chat.type))
                tasks[q.from_user.id] = check_task
                checking_user = q.from_user.id
                check_task.add_done_callback(
                    lambda completed: tasks.pop(checking_user, None)
                    if tasks.get(checking_user) is completed else None)
                return
            row = self.follow.db.db.execute(
                "SELECT * FROM follows WHERE id=? AND user_id=?", (follow_id, q.from_user.id)
            ).fetchone()
            if not row or (q.message.chat.type != "private" and row["chat_id"] != q.message.chat.id):
                await acknowledge(q, "Follow unavailable")
                return
            if action == "open":
                selected_features = list(json.loads(row["features"]))
                selected = ", ".join(selected_features) or "none"
                errors = {str(item["category"]): str(item["reason"])
                          for item in self.follow.db.db.execute(
                              "SELECT category,reason FROM follow_source_errors WHERE follow_id=?",
                              (follow_id,))}
                baselines = {str(item[0]) for item in self.follow.db.db.execute(
                    "SELECT category FROM follow_baselines WHERE follow_id=?", (follow_id,))}
                health_lines = []
                for key in selected_features:
                    reason = errors.get(key) or (errors.get("profile") if key in
                                                 {"bio", "name", "avatar"} else None)
                    if reason:
                        health_lines.append(f"⚠️ {html.escape(key)} · {html.escape(reason[:85])}")
                    elif key in baselines:
                        health_lines.append(f"✓ {html.escape(key)} · baseline recorded")
                    else:
                        health_lines.append(f"○ {html.escape(key)} · not verified")
                body = (f"👀 <b>@{html.escape(row['handle'])}</b> · {row['platform'].title()}\n"
                        f"Watching: {html.escape(selected)}\n"
                        f"Delivery: {html.escape(row['mode'])}\n\n"
                        "<b>Source status</b>\n" + "\n".join(health_lines))
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
                await acknowledge(q, "Checking public sources…")
                try:
                    async with self.follow.check_lock:
                        fresh = self.follow.db.db.execute(
                            "SELECT * FROM follows WHERE id=? AND user_id=?",
                            (follow_id, q.from_user.id),
                        ).fetchone()
                        if fresh is None:
                            return
                        health = await self.follow.check(fresh)
                    available = ", ".join(k for k, ok in health.items() if ok) or "none"
                    blocked = ", ".join(k for k, ok in health.items() if not ok)
                    status = f"🔄 Follow check complete. Accessible: {available}."
                    if blocked:
                        status += f"\nUnavailable from public sources: {blocked}."
                        failures = self.follow.db.db.execute(
                            "SELECT category,reason FROM follow_source_errors WHERE follow_id=?",
                            (follow_id,)).fetchall()
                        details = [f"{e['category']}: {html.escape(e['reason'])}" for e in failures]
                        if details:
                            status += "\n" + "\n".join(details[:7])
                except (ValueError, TimeoutError, OSError, httpx.HTTPError):
                    status = "⚠️ Public follow sources could not be checked right now. No change was inferred."
                await self.bot.send_message(q.message.chat.id, status)
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
            elif parts[2] == "fetch" and notice["kind"] == "avatar" and \
                    self.module_enabled("downloader", q.message.chat.id) and self.store.rate(
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
                "SELECT d.url,d.kind,d.value,f.platform,f.handle FROM follow_downloads d JOIN follows f ON f.id=d.follow_id "
                "WHERE d.token=? AND d.follow_id=? AND d.user_id=? AND f.user_id=? "
                "AND f.chat_id=? AND d.expires>?",
                (parts[2], parts[1], q.from_user.id, q.from_user.id, q.message.chat.id,
                 time.time())).fetchone()
            if not entry or not self.module_enabled("downloader", q.message.chat.id):
                await self.bot.send_message(q.message.chat.id, "⚠️ This follow item is unavailable.")
                return
            if not self.store.rate(q.from_user.id, "links", 12):
                await self.bot.send_message(q.message.chat.id, "⚠️ Please try again shortly.")
                return
            kind = str(entry["kind"])
            if kind in {"bio", "name"}:
                label = "Bio" if kind == "bio" else "Display name"
                await self.bot.send_message(q.message.chat.id,
                    f"👤 <b>@{html.escape(entry['handle'])}</b> · {label} snapshot\n\n"
                    f"{html.escape(str(entry['value']))}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🔗 View profile", url=entry["url"])
                    ]]))
                return
            if kind == "avatar":
                try:
                    target = Target(entry["platform"], entry["handle"], entry["url"])
                    snapshot = await asyncio.wait_for(public_profile(target, display_avatar=True), 25)
                    raw = snapshot.get("avatar")
                    if not raw:
                        raise ValueError("no public avatar URL")
                    photo = await profile_photo(raw)
                    await self.bot.send_document(q.message.chat.id, photo,
                        caption=f"👤 @{html.escape(entry['handle'])} · current profile photo")
                except (ValueError, TimeoutError, OSError, httpx.HTTPError, TelegramBadRequest):
                    await self.bot.send_message(q.message.chat.id,
                        "⚠️ The current profile photo is inaccessible. It may require login or have changed.")
                return
            if kind == "live":
                await self.bot.send_message(q.message.chat.id,
                    "🔴 This is a live stream. A complete video file may not exist until the "
                    "stream ends. Watch it on the original platform:",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🔗 Open live", url=entry["url"])
                    ]]))
                return
            if kind == "tweets":
                try:
                    post = await public_x_post(entry["url"])
                    if post and not post[1]:
                        text = f"📝 @{entry['handle']}\n\n{post[0]}"
                        for offset in range(0, len(text), 3900):
                            await self.bot.send_message(q.message.chat.id,
                                text[offset:offset + 3900], parse_mode=None)
                        return
                except (ValueError, TimeoutError, httpx.HTTPError):
                    post = None
                if post is None and entry["value"]:
                    # This excerpt was verified when the Follow event was seen.
                    # It is not advertised as the complete original tweet.
                    await self.bot.send_message(q.message.chat.id,
                        f"📝 <b>@{html.escape(entry['handle'])}</b> · saved text excerpt\n\n"
                        f"{html.escape(str(entry['value']))}\n\n"
                        "Attempting to fetch original media if available…")
            jid = self.store.create_job(q.from_user.id, q.message.chat.id, entry["url"])
            await self.bot.send_message(q.message.chat.id, "⏳ Preparing your download…",
                reply_markup=kb([[('Cancel ✕', f'j:{jid}:cancel')]]))
            return
        if data.startswith("m:"):
            await acknowledge(q)
            return
        if data.startswith("c:"):
            parts = data.split(":")
            if len(parts) != 3 or parts[2] not in {
                    "y", "n", "p", "q", "360", "480", "720", "1080", "audio"}:
                await acknowledge(q)
                return
            if parts[2] == "q":
                row = self.store.db.execute(
                    "SELECT url FROM confirmations WHERE id=? AND user_id=? AND chat_id=? "
                    "AND prompt_id=? AND state='PENDING' AND expires>?",
                    (parts[1], q.from_user.id, q.message.chat.id,
                     q.message.message_id, time.time())).fetchone()
                await acknowledge(q, "Request expired" if not row else None)
                if row and (youtube_video_url(row["url"]) or source(row["url"]) == "pornhub"):
                    message = q.message
                    assert message is not None and not isinstance(message, InaccessibleMessage)
                    provider = source(row["url"])
                    url = str(row["url"])

                    async def render_quality(sizes: dict[str, int]) -> None:
                        def quality_label(height: str, *, up_to: bool = False) -> str:
                            title = f"🎬 {'Up to ' if up_to else ''}{height}p"
                            return (f"{title} · {sizes[height] / 1_000_000:.1f} MB"
                                    if height in sizes else title)

                        size_note = ("Source-reported download bytes; final MP4 may differ after merging."
                                     if sizes else "Exact size appears when the source reports it.")
                        if provider == "pornhub":
                            await message.edit_text(
                                "🎛 <b>Pornhub quality</b>\nChoose the maximum video resolution.\n"
                                + size_note,
                                reply_markup=kb([[(quality_label("480"), f"c:{parts[1]}:480"),
                                                  (quality_label("720"), f"c:{parts[1]}:720")],
                                                 [(quality_label("1080", up_to=True), f"c:{parts[1]}:1080")],
                                                 [("✖ Cancel", f"c:{parts[1]}:n")]]))
                        else:
                            await message.edit_text(
                                "🎛 <b>YouTube quality</b>\nChoose video quality or MP3.\n"
                                + size_note,
                                reply_markup=kb([[(quality_label("360"), f"c:{parts[1]}:360"),
                                                  (quality_label("480"), f"c:{parts[1]}:480")],
                                                 [(quality_label("720"), f"c:{parts[1]}:720"),
                                                  (quality_label("1080", up_to=True), f"c:{parts[1]}:1080")],
                                                 [("🎵 MP3", f"c:{parts[1]}:audio")],
                                                 [("✖ Cancel", f"c:{parts[1]}:n")]]))

                    # Make quality choices available immediately. A slow source
                    # metadata query can enrich the same menu without blocking a tap.
                    await render_quality({})
                    worker = getattr(self, "worker", None)
                    if worker:
                        async def enrich_quality() -> None:
                            try:
                                sizes = await worker.quality_sizes(url)
                                if not sizes:
                                    return
                                pending = self.store.db.execute(
                                    "SELECT 1 FROM confirmations WHERE id=? AND user_id=? AND chat_id=? "
                                    "AND prompt_id=? AND state='PENDING' AND expires>?",
                                    (parts[1], q.from_user.id, message.chat.id,
                                     message.message_id, time.time())).fetchone()
                                if pending:
                                    await render_quality(sizes)
                            except Exception:
                                return  # Callback may have been used or message deleted.

                        quality_task = asyncio.create_task(enrich_quality())
                        quality_tasks = getattr(self, "_quality_tasks", None)
                        if quality_tasks is not None:
                            quality_tasks.add(quality_task)
                            quality_task.add_done_callback(quality_tasks.discard)
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
                    confirmation_creator = await resolve_creator_from_post(url)
                except (ValueError, TimeoutError, httpx.HTTPError):
                    confirmation_creator = None
                if confirmation_creator:
                    await self.show_profile(q.message.chat.id, q.from_user.id, confirmation_creator)
                else:
                    await self.bot.send_message(q.message.chat.id,
                        "Creator profile could not be identified from this public post.")
                return
            mode = parts[2] if parts[2] in {"360", "480", "720", "1080", "audio"} else "auto"
            state, decision_jid = self.store.decision(
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
                    reply_markup=kb([[("Cancel ✕", f"j:{decision_jid}:cancel")]]),
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
            if (action in {"q", "s"} and not self.module_enabled("downloader", q.message.chat.id)
                    or action == "follow" and not self.module_enabled("follow", q.message.chat.id)):
                return
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
                collection_creator = profile_target(
                    str(self.store.collection_context(cid).get("source_url") or "")
                )
                if collection_creator:
                    await self.show_follow(q.message.chat.id, q.from_user.id, collection_creator)
            elif action == "stop":
                stopped = self.store.cancel_collection(
                    cid, q.from_user.id, q.message.chat.id, q.message.message_id)
                if stopped is not None:
                    await self.render_collection(cid, q.from_user.id, q.message.chat.id, 0)
                    await self.bot.send_message(q.message.chat.id,
                        f"⏹ Stopped {stopped} pending items. The current download may finish.")
            elif action == "status":
                return
            elif action == "p" and arg.isdigit():
                number = int(arg)
                await self.render_collection(cid, q.from_user.id, q.message.chat.id, number)
            elif action == "q" and (arg == "all" or arg.isdigit() and int(arg) < len(items)):
                context = self.store.collection_context(cid)
                provider = str(context.get("kind") or "")
                target = items[int(arg)] if arg.isdigit() else items[0]
                is_video = provider == "youtube" or target.get("kind") == "video"
                quality_choices = (
                    [("🎬 360p", "360"), ("🎬 480p", "480"),
                     ("🎬 720p", "720"), ("🎬 Up to 1080p", "1080"),
                     ("🎵 MP3", "audio")]
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
                if mode not in {"auto", "audio", "360", "480", "720", "1080"}:
                    return
                indexes = range(len(items)) if arg == "all" else ([int(arg)] if arg.isdigit() else [])
                created_jobs = []
                for number in indexes:
                    if not 0 <= number < len(items):
                        continue
                    selected_jid, created = self.store.select_collection(
                        cid, q.from_user.id, q.message.chat.id, number, mode
                    )
                    if created and selected_jid:
                        created_jobs.append(selected_jid)
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
                job_creator = profile_target(str(context.get("profile_url") or "")) or creator_from_post(
                    url, context)
                if (not job_creator and source(url) == "instagram"
                        and self.instagram_relay is not None
                        and self.store.rate(q.from_user.id, "instagram_creator", 3)):
                    try:
                        handle = await self.instagram_relay.resolve_creator(url)
                        if handle:
                            job_creator = profile_target(f"https://www.instagram.com/{handle}/")
                            if job_creator:
                                context["profile_url"] = job_creator.url
                                self.store.set_job_context(parts[1], context)
                    except (ValueError, TimeoutError):
                        pass
                if not job_creator and self.store.rate(q.from_user.id, "profile", 8):
                    try:
                        if source(url) == "tiktok":
                            url = await self.worker._resolve_short(url)
                        job_creator = await resolve_creator_from_post(url, context)
                    except (ValueError, TimeoutError, httpx.HTTPError):
                        job_creator = None
                if job_creator:
                    if parts[2] == "profile":
                        await self.show_profile(q.message.chat.id, q.from_user.id, job_creator)
                    else:
                        await self.show_follow(q.message.chat.id, q.from_user.id, job_creator)
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
                    download_url = clean_url(str(context.get("avatar_url") or ""))
                    mode = "document"
                    item_context = {
                        "kind": "profile_avatar", "source_url": profile_url,
                        "title": context.get("title"), "artist": context.get("artist"),
                    }
                elif parts[2] == "latest":
                    try:
                        profile = profile_target(profile_url)
                        if profile is None:
                            return
                        posts = await public_items(profile)
                        download_url = next(url for _kind, url, _title in posts)
                    except (ValueError, TimeoutError, StopIteration):
                        await self.bot.send_message(
                            q.message.chat.id,
                            "The latest post could not be read from public sources. The platform may require login."
                        )
                        return
                    mode = "auto"
                    item_context = {"source_url": download_url, "kind": "post"}
                else:
                    username = str(context.get("artist") or "")
                    story_target = profile_target(profile_url)
                    if not story_target or story_target.handle != username:
                        return
                    await self.show_stories(q.message.chat.id, q.from_user.id, story_target)
                    return
                jid = self.store.create_job(
                    q.from_user.id, q.message.chat.id, download_url, mode, item_context
                )
                progress_message = await self.bot.send_message(
                    q.message.chat.id, "⏳ Checking public media…",
                    reply_markup=kb([[("Cancel ✕", f"j:{jid}:cancel")]]),
                )
                self.store.bind_progress(jid, progress_message.message_id)
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
                derivative_jid, created = self.store.create_derivative(
                    parts[1], q.from_user.id, q.message.chat.id, "audio"
                )
                if created and derivative_jid is not None:
                    if q.message.document or q.message.video:
                        await q.message.edit_reply_markup(reply_markup=None)
                        derivative_progress = await self.bot.send_message(
                            q.message.chat.id, "🎵 Preparing MP3…"
                        )
                        self.store.bind_progress(derivative_jid, derivative_progress.message_id)
                    else:
                        await q.message.edit_text("🎵 Preparing MP3…")
                        self.store.bind_progress(derivative_jid, q.message.message_id)
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
        if self.settings.mika_n8n_telegram_relay:
            # Switching from n8n's Telegram Trigger back to the bot's sole
            # polling consumer must preserve pending updates.
            webhook = await self.bot.get_webhook_info()
            if webhook.url:
                await self.bot.delete_webhook(drop_pending_updates=False)
        identity = await self.bot.get_me()
        self.bot_username = identity.username or ""
        await self.audio_ui.recover()
        await self.bot.set_my_commands(
            [
                BotCommand(command=x, description=y)
                for x, y in (
                    ("help", "How to use"),
                    ("search", "Search music and podcasts"),
                    ("music", "Search music"),
                    ("podcast", "Search podcasts"),
                    ("book", "Find books in local sources"),
                    ("twitter", "Twitter link help"),
                    ("instagram", "Instagram link help"),
                    ("tiktok", "TikTok link help"),
                    ("youtube", "YouTube link help"),
                    ("follow", "Follow a public profile"),
                    ("explore", "Discover TikTok posts"),
                    ("following", "Manage followed profiles"),
                    ("settings", "Preferences"),
                    ("queue", "My jobs"),
                    ("status", "Status"),
                    ("fal", "Reviewed Hafez fortune"),
                    ("pishi", "Reviewed domestic cat photos"),
                    ("earth", "Species and nature"),
                    ("art", "Open-access art"),
                    ("xotd", "Daily sourced features"),
                    ("quote", "Sourced Wikiquote excerpts"),
                    ("wiki", "Wikipedia search and discovery"),
                    ("tts", "Configure text to speech"),
                    ("stt", "Configure speech to text"),
                    ("poem", "Poetry menu"),
                    ("dice", "Roll a Telegram die"),
                    ("rules", "Group rules"),
                    ("groupstatus", "Observed group message statistics"),
                    ("id", "Identify a replied group member"),
                    ("notes", "Manage group notes"),
                    ("qr", "Create a QR image"),
                    ("url", "Get a link for a replied file"),
                    ("mika", "Ask Mikachan through n8n"),
                    ("scan", "Scan a replied QR or barcode"),
                    ("ocr", "Read text in a replied image"),
                    ("st", "Convert replied image to sticker"),
                    ("xst", "Convert replied sticker to PNG"),
                    ("weather", "Weather and forecast"),
                    ("now", "Local time and calendars"),
                    ("nasa", "NASA astronomy picture"),
                    ("proxy", "Current Telegram proxies"),
                    ("vpn", "MikaSub and subscription converter"),
                )
            ]
        )
        task = asyncio.create_task(self.worker.loop())
        follow_task = asyncio.create_task(self.follow.loop())
        self.mikasub_core.background = asyncio.create_task(self.mikasub_core.periodic())
        try:
            await self.dp.start_polling(self.bot)
        finally:
            self.follow.running = False
            for check_task in self._check_all_tasks.values():
                check_task.cancel()
            await asyncio.gather(*self._check_all_tasks.values(), return_exceptions=True)
            for quality_task in self._quality_tasks:
                quality_task.cancel()
            await asyncio.gather(*self._quality_tasks, return_exceptions=True)
            follow_task.cancel()
            await asyncio.gather(follow_task, return_exceptions=True)
            await self.worker.stop()
            task.cancel()
            await self.settings_ui.close()
            await self.mikasub.close()
            await self.mikasub_core.close()
            await self.utilities.close()
            await self.content_ui.providers.close()
            await self.audio_ui.close()
            await self.n8n_gateway.close()
            if self.userbot:
                await self.userbot.close()
            await self.bot.session.close()
