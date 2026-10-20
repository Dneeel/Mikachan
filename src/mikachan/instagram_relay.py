"""Private, bounded presentation of verified InstaRoxinoBot results."""

import asyncio
import html
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from .media import media_kind, verify
from .security import clean_url

_FOOTER = re.compile(r"(?im)^\s*🤖\s*Downloaded with @instaRoxinoBot\s*$")
_OPEN_POST = re.compile(r"(?im)^\s*(?:\[🔗 Open post\]\((https://[^)]+)\)|🔗 Open post)\s*$")
_IG_LINK = re.compile(r"https://(?:www\.)?instagram\.com/(?:p|reel|stories)/[^\s)<>]+", re.I)
_SOUND_PATH = re.compile(r"/(?:audio|music)/", re.I)


def instagram_media_link(raw: str) -> str | None:
    try:
        url = clean_url(raw)
    except ValueError:
        return None
    parts = urlsplit(url)
    if parts.hostname not in {"instagram.com", "www.instagram.com"}:
        return None
    if not re.match(r"^/(?:p|reel|reels|tv|stories)/[^/]+", parts.path, re.I):
        return None
    if _SOUND_PATH.search(parts.path):
        return None
    return url


def clean_instagram_caption(raw: str) -> tuple[str, str | None]:
    """Remove the provider footer and its post-link line; retain author text."""
    link = None
    for match in _IG_LINK.finditer(raw):
        link = instagram_media_link(match.group().rstrip(".,")) or link
    kept = []
    for line in raw.splitlines():
        if _FOOTER.fullmatch(line) or _OPEN_POST.fullmatch(line):
            continue
        kept.append(line.rstrip())
    return "\n".join(kept).strip(), link


def _message_link(message) -> str | None:
    raw = str(getattr(message, "raw_text", "") or "")
    _caption, link = clean_instagram_caption(raw)
    if link:
        return link
    for entity in getattr(message, "entities", None) or []:
        candidate = getattr(entity, "url", None)
        if isinstance(candidate, str) and (result := instagram_media_link(candidate)):
            return result
    return None


def _recognized_media(message) -> bool:
    raw = str(getattr(message, "raw_text", "") or "")
    if "Downloaded with @instaRoxinoBot" in raw:
        return True
    return any("get the caption" in _button_label(button).casefold()
               for row in getattr(message, "buttons", None) or [] for button in row)


def _unsupported_response(messages: list) -> str:
    text = " ".join(str(getattr(item, "raw_text", "") or "").casefold()
                    for item in messages)[:2000]
    if any(word in text for word in ("slow down", "too fast", "try again later",
                                     "wait 29", "wait 30", "لطفا آرام", "لطفا آروم")):
        return "Instagram provider asked us to slow down. Try again shortly."
    return "Sorry, this Instagram response is not supported."


def _button_label(button) -> str:
    return str(getattr(button, "text", "") or "").strip()


def _action_for(label: str) -> str | None:
    lowered = label.casefold()
    if "download stories" in lowered:
        return "stories"
    if "download highlights" in lowered:
        return "highlights"
    if "download posts" in lowered:
        return "posts"
    if "next 5 posts" in lowered:
        return "next_posts"
    if lowered.startswith(("next", "➡")):
        return "next_page"
    if lowered.startswith(("previous", "prev", "back", "⬅")):
        return "previous_page"
    return None


@dataclass
class Menu:
    user: int
    chat: int
    message: int
    upstream: int
    handle: str
    expires: float
    buttons: dict[str, tuple[int, int, str]]


@dataclass
class MediaAction:
    user: int
    chat: int
    message: int
    upstream: int
    expires: float


class InstagramRelay:
    def __init__(self, bridge, bot, settings, worker, on_follow=None):
        self.bridge, self.bot, self.settings, self.worker = bridge, bot, settings, worker
        self.on_follow = on_follow
        self.menus: dict[str, Menu] = {}
        self.media_actions: dict[str, MediaAction] = {}
        self.audio_gate = asyncio.Semaphore(2)
        self.audio_active: set[str] = set()
        self.active_menus: set[str] = set()

    @staticmethod
    def _media(message) -> bool:
        return bool(getattr(message, "photo", None) or getattr(message, "document", None)
                    or getattr(message, "video", None))

    @staticmethod
    def _media_size(message) -> int:
        file = getattr(message, "file", None)
        document = getattr(message, "document", None)
        return int(getattr(file, "size", None) or getattr(document, "size", None) or 0)

    async def fetch_url(self, url: str, directory: Path, max_bytes: int,
                        *, audio_only: bool = False) -> tuple[list[Path], str]:
        if not instagram_media_link(url):
            raise ValueError("Sorry, Instagram sound and unsupported links are not supported")
        messages = await self.bridge.instagram_request(url, settle=12)
        media = [message for message in messages if self._media(message)]
        if media and not any(_recognized_media(message) for message in media):
            raise ValueError("Sorry, this Instagram media response is not supported")
        if not media or len(media) > 31 or audio_only and len(media) != 1:
            raise ValueError(_unsupported_response(messages))
        paths: list[Path] = []
        caption = ""
        for index, message in enumerate(media):
            if self._media_size(message) > max_bytes:
                raise ValueError("Instagram media exceeds the Telegram upload limit")
            message = await self.bridge.instagram_caption(message)
            content, _ = clean_instagram_caption(str(getattr(message, "raw_text", "") or ""))
            caption = caption or content
            path = await self.bridge.instagram_download(message, directory / f"ig-{index:02d}",
                                                        max_bytes)
            await verify(path, max_bytes)
            paths.append(path)
        return paths, caption

    @staticmethod
    def _profile_text(message, handle: str) -> str:
        raw = str(getattr(message, "raw_text", "") or "")
        def field(name: str) -> str:
            match = re.search(rf"(?im)^\s*[^\w\n]*{name}:\s*(.+)$", raw)
            return match.group(1).strip(" `*\r")[:450] if match else ""
        name = field("Name") or handle
        bio_match = re.search(
            r"(?ims)^\s*[^\w\n]*Biography:\s*(.*?)(?=^\s*[^\w\n]*(?:Name|Followers|Following|Posts|Page status):|\Z)",
            raw)
        bio = bio_match.group(1).strip(" \n`*\r")[:450] if bio_match else ""
        followers, following, posts = field("Followers"), field("Following"), field("Posts")
        status = field("Page status")
        lines = [f"🆔 Username: <code>{html.escape(handle)}</code>"]
        if bio:
            lines += ["✏️ Biography:", f"<code>{html.escape(bio)}</code>", ""]
        lines += [f"🧍 Name: <code>{html.escape(name)}</code>",
                  f"👫 Followers: <b>{html.escape(followers or '?')}</b>",
                  f"👫 Following: <b>{html.escape(following or '?')}</b>",
                  f"🔔 Posts: <b>{html.escape(posts or '?')}</b>",
                  f"📋 Page status: <b>{html.escape(status or 'Unknown')}</b>"]
        return "\n".join(lines)[:1000]

    def _menu_markup(self, token: str, upstream, *, highlight_list: bool = False,
                     profile_handle: str = "") -> tuple[InlineKeyboardMarkup,
                                                            dict[str, tuple[int, int, str]]]:
        rows = []
        actions: dict[str, tuple[int, int, str]] = {}
        for ri, row in enumerate((getattr(upstream, "buttons", None) or [])[:25]):
            buttons = []
            for ci, button in enumerate(row[:8]):
                label = _button_label(button)
                if getattr(button, "data", None) is None:
                    continue
                action = _action_for(label)
                if highlight_list and action is None and len(label) <= 70 and not re.search(
                        r"audio|song|youtube|pinterest|tiktok|twitter|back", label, re.I):
                    action = f"h{len(actions)}"  # A selectable highlight title.
                if action and action not in actions:
                    actions[action] = (ri, ci, label)
                    display = {"stories": "⭕ Stories", "highlights": "🎞 Highlights",
                               "posts": "📸 Posts"}.get(action, label[:64]) if profile_handle else label[:64]
                    buttons.append(InlineKeyboardButton(text=display,
                                                        callback_data=f"ir:{token}:{action}"))
            if buttons:
                rows.append(buttons)
        if profile_handle:
            rows.insert(0, [InlineKeyboardButton(text="👀 Follow",
                                                 callback_data=f"ir:{token}:follow")])
            rows.append([InlineKeyboardButton(text="🔗 Open profile",
                url=f"https://www.instagram.com/{profile_handle}/")])
            actions["follow"] = (-1, -1, "")
        else:
            rows.append([InlineKeyboardButton(text="✖ Close", callback_data=f"ir:{token}:close")])
        return InlineKeyboardMarkup(inline_keyboard=rows), actions

    def _remember_menu(self, token: str, user: int, chat: int, message: int,
                       handle: str, upstream, actions: dict[str, tuple[int, int, str]]) -> None:
        now = time.time()
        self.menus = {k: v for k, v in self.menus.items() if v.expires > now}
        if len(self.menus) >= 100:
            self.menus.pop(next(iter(self.menus)))
        self.menus[token] = Menu(user, chat, message, upstream.id, handle, now + 900, actions)

    async def show_profile(self, chat: int, user: int, handle: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle):
            raise ValueError("Invalid Instagram username")
        messages = await self.bridge.instagram_request("@" + handle, settle=2)
        card = next((item for item in messages if getattr(item, "buttons", None)
                     and self._media(item)), None)
        if card is None:
            raise ValueError(_unsupported_response(messages))
        token = secrets.token_urlsafe(8)
        markup, actions = self._menu_markup(token, card, profile_handle=handle)
        if not {"stories", "highlights", "posts"} & actions.keys():
            raise ValueError("Instagram profile controls are unavailable")
        body = self._profile_text(card, handle)
        if self._media_size(card) > 10_000_000:
            raise ValueError("Instagram profile image is too large")
        root = self.settings.data_dir / "jobs"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ig-profile-", dir=root) as temp:
            path = await self.bridge.instagram_download(card, Path(temp) / "profile", 10_000_000)
            await verify(path, 10_000_000)
            sent = await self.bot.send_photo(chat, FSInputFile(path), caption=body,
                                             reply_markup=markup)
        self._remember_menu(token, user, chat, sent.message_id, handle, card, actions)

    async def _send_media(self, chat: int, user: int, message, fallback_url: str | None,
                          *, trusted_batch: bool = False) -> bool:
        if not trusted_batch and not _recognized_media(message):
            return False
        if self._media_size(message) > 48 * 1024 * 1024:
            return False
        message = await self.bridge.instagram_caption(message)
        caption, discovered = clean_instagram_caption(str(getattr(message, "raw_text", "") or ""))
        source_url = _message_link(message) or discovered or instagram_media_link(fallback_url or "")
        root = self.settings.data_dir / "jobs"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ig-item-", dir=root) as temp:
            path = await self.bridge.instagram_download(message, Path(temp) / "media",
                                                        48 * 1024 * 1024)
            path = (await self.worker._prepare_media([path], 48 * 1024 * 1024))[0]
            check = await verify(path, 48 * 1024 * 1024)
            kind = media_kind(path, check)
            token = secrets.token_urlsafe(8)
            rows = ([[InlineKeyboardButton(
                text="🔗 Open stories" if "/stories/" in source_url else "🔗 Open post",
                url=source_url)] ] if source_url else [])
            if kind == "video" and check["audio"]:
                rows.append([InlineKeyboardButton(text="🎵 MP3",
                    callback_data=f"ir:{token}:mp3")])
            markup = InlineKeyboardMarkup(inline_keyboard=rows)
            safe_caption = html.escape(caption[:700]) if caption else None
            if kind == "photo":
                sent = await self.bot.send_photo(chat, FSInputFile(path),
                                                 caption=safe_caption, reply_markup=markup)
            elif kind == "video":
                sent = await self.bot.send_video(chat, FSInputFile(path),
                                                 caption=safe_caption, reply_markup=markup,
                                                 supports_streaming=True)
            else:
                return False
        if kind == "video" and check["audio"]:
            now = time.time()
            self.media_actions = {key: item for key, item in self.media_actions.items()
                                  if item.expires > now}
            if len(self.media_actions) >= 200:
                self.media_actions.pop(next(iter(self.media_actions)))
            self.media_actions[token] = MediaAction(user, chat, sent.message_id,
                                                     message.id, now + 900)
        return True

    async def _send_results(self, chat: int, user: int, handle: str,
                            messages: list, *, stories: bool = False) -> tuple[int, object | None, int]:
        fallback = f"https://www.instagram.com/stories/{handle}/" if stories else None
        count = 0
        skipped = 0
        menu = None
        trusted_batch = any(self._media(message) and _recognized_media(message)
                            for message in messages)
        for message in messages:
            if self._media(message) and trusted_batch:
                if await self._send_media(chat, user, message, fallback,
                                          trusted_batch=True):
                    count += 1
                else:
                    skipped += 1
            elif getattr(message, "buttons", None):
                menu = message
        return count, menu, skipped

    async def callback(self, query) -> bool:
        data = str(query.data or "")
        if not data.startswith("ir:"):
            return False
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Invalid Instagram action")
            return True
        token, action = parts[1:]
        if action == "mp3":
            item = self.media_actions.get(token)
            if (not item or item.expires < time.time() or item.user != query.from_user.id
                    or item.chat != query.message.chat.id
                    or item.message != query.message.message_id):
                await query.answer("Media action expired or belongs to another user")
                return True
            if token in self.audio_active:
                await query.answer("MP3 is already being prepared")
                return True
            await query.answer("Preparing MP3…")
            self.audio_active.add(token)
            try:
                async with self.audio_gate:
                    root = self.settings.data_dir / "jobs"
                    root.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix="ig-audio-", dir=root) as temp:
                        path = await self.bridge.instagram_download(
                            type("Source", (), {"id": item.upstream})(), Path(temp) / "media",
                            240 * 1024 * 1024)
                        if path.stat().st_size > 240 * 1024 * 1024:
                            raise ValueError("Instagram video exceeds the MP3 conversion limit")
                        output = await self.worker._transcode_mp3(path, 48 * 1024 * 1024)
                        await self.bot.send_audio(item.chat, FSInputFile(output))
            except Exception:
                await self.bot.send_message(item.chat, "⚠️ MP3 conversion failed. Try again later.")
            finally:
                self.audio_active.discard(token)
            return True
        menu = self.menus.get(token)
        if (not menu or menu.expires < time.time() or menu.user != query.from_user.id
                or menu.chat != query.message.chat.id
                or menu.message != query.message.message_id):
            await query.answer("Instagram menu expired or belongs to another user")
            return True
        if action == "close":
            await query.answer()
            self.menus.pop(token, None)
            try:
                await query.message.delete()
            except Exception:
                await query.message.edit_reply_markup(reply_markup=None)
            return True
        if action == "follow":
            if self.on_follow is None:
                await query.answer("Follow is unavailable")
                return True
            from .follow import profile_target
            target = profile_target(f"https://www.instagram.com/{menu.handle}/")
            if target is None:
                await query.answer("Profile unavailable")
                return True
            await query.answer()
            await self.on_follow(menu.chat, menu.user, target)
            return True
        selected = menu.buttons.get(action)
        if not selected:
            await query.answer("This option is unavailable")
            return True
        if token in self.active_menus:
            await query.answer("This Instagram request is already running")
            return True
        self.active_menus.add(token)
        await query.answer("Loading…")
        try:
            settle = (20 if action == "stories" or action.startswith("h") else
                      12 if action in {"posts", "next_posts"} else 3)
            messages = await self.bridge.instagram_click(menu.upstream, *selected[:2], selected[2],
                settle=settle, stop_on_highlight_done=action.startswith("h"))
            count, next_menu, skipped = await self._send_results(
                menu.chat, menu.user, menu.handle, messages, stories=action == "stories")
            if next_menu:
                replacement = secrets.token_urlsafe(8)
                markup, actions = self._menu_markup(replacement, next_menu,
                                                    highlight_list=action in {"highlights", "next_page",
                                                                              "previous_page"})
                heading = ("🗂 <b>Highlights</b> · choose one" if action == "highlights"
                           else f"✅ Sent {count} posts. Choose an option below." if count
                           else "Choose an option below.")
                if skipped:
                    heading += f"\n⚠️ {skipped} media items exceeded the upload limit."
                if getattr(query.message, "photo", None):
                    sent = await self.bot.send_message(menu.chat, heading, reply_markup=markup)
                else:
                    await query.message.edit_text(heading, reply_markup=markup)
                    sent = query.message
                    self.menus.pop(token, None)
                self._remember_menu(replacement, menu.user, menu.chat, sent.message_id,
                                    menu.handle, next_menu, actions)
            elif count:
                if skipped or len(messages) >= 110:
                    await self.bot.send_message(menu.chat,
                        f"⚠️ Delivered {count} items; {skipped} exceeded the upload limit. "
                        "The provider batch may be incomplete.")
                else:
                    await self.bot.send_message(menu.chat, "✅ Done" if action == "stories"
                                                else "✅ Highlight downloaded" if action.startswith("h")
                                                else f"✅ Sent {count} posts.")
            else:
                await self.bot.send_message(menu.chat, _unsupported_response(messages))
        except Exception:
            await self.bot.send_message(menu.chat,
                "⚠️ Instagram provider could not complete this action. Try again later.")
        finally:
            self.active_menus.discard(token)
        return True
