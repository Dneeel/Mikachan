"""Private, bounded polling of public creator activity.

This module keeps monitoring state separate from downloader jobs. Extractors can
fail or require login; a failed poll never advances the baseline or invents an
event. No account credentials are used.
"""

import asyncio
import html
import json
import os
import re
import secrets
import signal
import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .catalog import _Meta
from .security import allowed, clean_url, source, validate_dns
from .store import Store

INTERVAL = 15 * 60
MAX_FOLLOWS = 25
FEATURES = {
    "instagram": ("posts", "reels", "stories", "bio", "name", "avatar"),
    "twitter": ("tweets", "bio", "name", "avatar"),
    "youtube": ("videos", "shorts"),
    "tiktok": ("posts", "stories", "bio", "name", "avatar"),
}
LABELS = {
    "posts": "Posts", "reels": "Reels", "stories": "Stories (best effort)",
    "tweets": "Tweets", "videos": "Videos", "shorts": "Shorts", "bio": "Bio changes",
    "name": "Display name changes", "avatar": "Avatar changes",
}


@dataclass(frozen=True)
class Target:
    platform: str
    handle: str
    url: str


def profile_target(raw: str) -> Target | None:
    """Accept canonical public profile URLs, never an arbitrary extractor URL."""
    try:
        url = clean_url(raw)
    except ValueError:
        return None
    platform = source(url)
    parts = [x for x in urlsplit(url).path.split("/") if x]
    if platform not in FEATURES or not parts:
        return None
    handle = ""
    if platform in {"instagram", "twitter"} and len(parts) == 1:
        handle = parts[0].lstrip("@")
    elif platform == "tiktok" and len(parts) == 1 and parts[0].startswith("@"):
        handle = parts[0][1:]
    elif platform == "youtube" and parts[0].startswith("@") and (
            len(parts) == 1 or len(parts) == 2 and parts[1] in {"videos", "shorts", "streams"}):
        handle = parts[0][1:]
    elif platform == "youtube" and parts[0] in {"channel", "user", "c"} and (
            len(parts) == 2 or len(parts) == 3 and parts[2] in {"videos", "shorts", "streams"}):
        handle = parts[1]
    if not re.fullmatch(r"[\w.-]{1,64}", handle, re.ASCII):
        return None
    if handle.lower() in {"p", "reel", "reels", "stories", "explore", "accounts",
                          "direct", "status", "home", "search", "watch", "shorts"}:
        return None
    if platform == "instagram":
        canonical = f"https://www.instagram.com/{handle}/"
    elif platform == "twitter":
        canonical = f"https://x.com/{handle}"
    elif platform == "tiktok":
        canonical = f"https://www.tiktok.com/@{handle}"
    elif parts[0] == "channel":
        canonical = f"https://www.youtube.com/channel/{handle}"
    elif parts[0] == "user":
        canonical = f"https://www.youtube.com/user/{handle}"
    elif parts[0] == "c":
        canonical = f"https://www.youtube.com/c/{handle}"
    else:
        canonical = f"https://www.youtube.com/@{handle}"
    return Target(platform, handle, canonical)


def command_target(arg: str) -> Target | None:
    parts = arg.strip().split()
    if len(parts) == 1:
        return profile_target(parts[0])
    if len(parts) != 2:
        return None
    platform, handle = parts[0].lower(), parts[1].lstrip("@")
    if platform in {"x", "twitter"}:
        return profile_target(f"https://x.com/{handle}")
    if platform in {"ig", "instagram"}:
        return profile_target(f"https://www.instagram.com/{handle}/")
    if platform in {"tt", "tiktok"}:
        return profile_target(f"https://www.tiktok.com/@{handle}")
    if platform in {"yt", "youtube"}:
        return profile_target(f"https://www.youtube.com/@{handle}")
    return None


def creator_from_post(url: str, meta: dict[str, object] | None = None) -> Target | None:
    meta = meta or {}
    platform = source(url)
    parts = [x for x in urlsplit(url).path.split("/") if x]
    if platform in {"twitter", "tiktok"} and len(parts) >= 3:
        return profile_target(f"https://{'x.com' if platform == 'twitter' else 'www.tiktok.com'}/{parts[0]}")
    candidate = meta.get("uploader_url") or meta.get("channel_url") or meta.get("profile_url")
    if isinstance(candidate, str):
        found = profile_target(candidate)
        if found:
            return found
    if platform == "instagram":
        candidate = meta.get("username") or meta.get("uploader")
        if (isinstance(candidate, str) and re.fullmatch(r"[\w.]{1,30}", candidate)
                and not candidate.isdigit()):
            return profile_target(f"https://www.instagram.com/{candidate}/")
    return None


class FollowStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.executescript("""
        CREATE TABLE IF NOT EXISTS follow_sessions (
          id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
          message_id INTEGER, platform TEXT NOT NULL, handle TEXT NOT NULL,
          url TEXT NOT NULL, stage TEXT NOT NULL, features TEXT NOT NULL,
          mode TEXT NOT NULL, destination TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS follows (
          id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
          platform TEXT NOT NULL, handle TEXT NOT NULL, url TEXT NOT NULL,
          features TEXT NOT NULL, mode TEXT NOT NULL, baseline INTEGER NOT NULL DEFAULT 0,
          profile_data TEXT NOT NULL DEFAULT '{}', next_check REAL NOT NULL,
          failures INTEGER NOT NULL DEFAULT 0,
          UNIQUE(user_id,url));
        CREATE TABLE IF NOT EXISTS follow_seen (
          follow_id TEXT NOT NULL, event_key TEXT NOT NULL,
          PRIMARY KEY(follow_id,event_key));
        CREATE TABLE IF NOT EXISTS follow_baselines (
          follow_id TEXT NOT NULL, category TEXT NOT NULL,
          PRIMARY KEY(follow_id,category));
        CREATE INDEX IF NOT EXISTS follows_due ON follows(next_check);
        CREATE TABLE IF NOT EXISTS follow_downloads (
          token TEXT PRIMARY KEY, follow_id TEXT NOT NULL, user_id INTEGER NOT NULL,
          url TEXT NOT NULL, expires REAL NOT NULL);
        """)

    def session(self, user: int, chat: int, target: Target) -> str:
        token = secrets.token_urlsafe(8)
        defaults = [x for x in FEATURES[target.platform] if x not in {"stories", "bio", "name", "avatar"}]
        self.db.execute("INSERT INTO follow_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (token, user, chat, None, target.platform, target.handle,
                         target.url, "confirm", json.dumps(defaults), "notice", "dm", time.time() + 1800))
        return token

    def bind(self, token: str, message: int) -> None:
        self.db.execute("UPDATE follow_sessions SET message_id=? WHERE id=?", (message, token))

    def get_session(self, token: str, user: int, chat: int, message: int):
        return self.db.execute("SELECT * FROM follow_sessions WHERE id=? AND user_id=? "
                               "AND chat_id=? AND message_id=? AND expires>?",
                               (token, user, chat, message, time.time())).fetchone()

    def save(self, session) -> str:
        target = session["url"]
        existing = self.db.execute("SELECT id FROM follows WHERE user_id=? AND url=?",
                                   (session["user_id"], target)).fetchone()
        if not existing and self.db.execute("SELECT count(*) FROM follows WHERE user_id=?",
                                            (session["user_id"],)).fetchone()[0] >= MAX_FOLLOWS:
            raise ValueError(f"You can follow up to {MAX_FOLLOWS} profiles.")
        token = existing[0] if existing else secrets.token_urlsafe(8)
        destination_chat = session["chat_id"] if session["destination"] == "here" else session["user_id"]
        self.db.execute("INSERT INTO follows(id,user_id,chat_id,platform,handle,url,features,mode,next_check) "
                        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,url) DO UPDATE SET "
                        "features=excluded.features,mode=excluded.mode,chat_id=excluded.chat_id",
                        (token, session["user_id"], destination_chat, session["platform"],
                         session["handle"], target, session["features"], session["mode"], time.time()))
        self.db.execute("DELETE FROM follow_sessions WHERE id=?", (session["id"],))
        return token

    def remove(self, token: str, user: int) -> bool:
        row = self.db.execute("DELETE FROM follows WHERE id=? AND user_id=? RETURNING id",
                              (token, user)).fetchone()
        if row:
            self.db.execute("DELETE FROM follow_seen WHERE follow_id=?", (token,))
            self.db.execute("DELETE FROM follow_baselines WHERE follow_id=?", (token,))
        return bool(row)


def confirm_panel(target: Target, token: str) -> tuple[str, InlineKeyboardMarkup]:
    notes = {
        "instagram": "Public posts and reels; stories are best effort. Live alerts are unavailable.",
        "twitter": "Public tweets and visible profile changes. X may block anonymous access.",
            "youtube": "Public channel videos and Shorts. Live start alerts are unavailable.",
        "tiktok": "Public posts; stories are best effort. Live alerts are unavailable.",
    }
    body = (f"👀 <b>Follow @{html.escape(target.handle)}</b> · {target.platform.title()}\n\n"
            f"{notes[target.platform]}\nChecks run about every 15 minutes. "
            "Only public content is monitored. Continue?")
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yes, follow", callback_data=f"f:{token}:yes"),
         InlineKeyboardButton(text="Cancel", callback_data=f"f:{token}:no")],
        [InlineKeyboardButton(text="🔗 Profile", url=target.url)],
    ])
    return body, markup


def settings_panel(session) -> tuple[str, InlineKeyboardMarkup]:
    selected = set(json.loads(session["features"]))
    token = session["id"]
    rows = [[InlineKeyboardButton(text=f"{'✅' if key in selected else '⬜'} {LABELS[key]}",
                                 callback_data=f"f:{token}:t:{key}")]
            for key in FEATURES[session["platform"]]]
    rows.append([InlineKeyboardButton(text=f"{'✅' if session['mode'] == 'media' else '⬜'} Send media",
                                    callback_data=f"f:{token}:m:media"),
                 InlineKeyboardButton(text=f"{'✅' if session['mode'] == 'notice' else '⬜'} Notify + link",
                                    callback_data=f"f:{token}:m:notice")])
    if session["chat_id"] != session["user_id"]:
        rows.append([InlineKeyboardButton(text=f"{'✅' if session['destination'] == 'dm' else '⬜'} Private chat",
                                        callback_data=f"f:{token}:d:dm"),
                     InlineKeyboardButton(text=f"{'✅' if session['destination'] == 'here' else '⬜'} This group",
                                        callback_data=f"f:{token}:d:here")])
    rows.append([InlineKeyboardButton(text="💾 Save Follow", callback_data=f"f:{token}:save")])
    rows.append([InlineKeyboardButton(text="Cancel", callback_data=f"f:{token}:no")])
    body = (f"👀 <b>Follow @{html.escape(session['handle'])}</b>\n"
            "Choose what to watch and how to receive it. Tap a category to toggle it.\n\n"
            "Stories depend on public extractor access. Username changes and live starts "
            "cannot be monitored reliably without account access.\n"
            "Media mode queues public posts for download; profile changes always arrive as text.")
    return body, InlineKeyboardMarkup(inline_keyboard=rows)


async def _extract(command: list[str], timeout: int) -> object:
    proc = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True, env={"PATH": os.environ.get("PATH", "")})
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        os.killpg(proc.pid, signal.SIGTERM)
        await proc.wait()
        raise
    if proc.returncode or len(stdout) > 3_000_000:
        raise ValueError("public source unavailable")
    return json.loads(stdout)


def _gallery_items(data: object, target: Target, kind: str) -> list[tuple[str, str, str]]:
    if not isinstance(data, list):
        raise ValueError("invalid gallery response")
    found: dict[str, tuple[str, str, str]] = {}
    for message in data:
        if not isinstance(message, list) or len(message) < 3 or not isinstance(message[-1], dict):
            continue
        meta = message[-1]
        if target.platform == "instagram":
            item_id = (meta.get("post_id") if kind == "stories" else meta.get("post_shortcode"))
            event_kind = "stories" if kind == "stories" else (
                "reels" if meta.get("type") == "reel" else "posts")
            url = (f"https://www.instagram.com/stories/{target.handle}/{item_id}/" if kind == "stories"
                   else f"https://www.instagram.com/{'reel' if event_kind == 'reels' else 'p'}/{item_id}/")
        elif target.platform == "twitter":
            item_id = meta.get("tweet_id")
            event_kind = "tweets"
            url = f"https://x.com/{target.handle}/status/{item_id}"
        else:
            item_id = meta.get("id")
            event_kind = "stories" if kind == "stories" else "posts"
            url = (f"https://www.tiktok.com/@{target.handle}/story/{item_id}" if kind == "stories"
                   else f"https://www.tiktok.com/@{target.handle}/video/{item_id}")
        if not item_id or meta.get("type") == "avatar":
            continue
        try:
            url = clean_url(url)
        except ValueError:
            continue
        found[str(item_id)] = (event_kind, url, str(meta.get("description") or meta.get("title") or "")[:300])
    return list(found.values())[:15]


async def public_items(target: Target, kind: str = "posts") -> list[tuple[str, str, str]]:
    await asyncio.to_thread(validate_dns, target.url)
    if target.platform == "youtube":
        data = await _extract(["yt-dlp", "--flat-playlist", "--playlist-end", "15",
                               "--dump-single-json", "--skip-download", "--no-warnings",
                               target.url.rstrip("/") + ("/shorts" if kind == "shorts" else "/videos")], 35)
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise ValueError("invalid channel response")
        items = []
        for entry in data["entries"][:15]:
            if isinstance(entry, dict) and re.fullmatch(r"[\w-]{11}", str(entry.get("id") or "")):
                items.append((kind if kind == "shorts" else "videos",
                              f"https://www.youtube.com/watch?v={entry['id']}",
                              str(entry.get("title") or "Video")[:300]))
        return items
    if target.platform == "instagram":
        url = (f"https://www.instagram.com/stories/{target.handle}/" if kind == "stories"
               else target.url)
    elif target.platform == "tiktok":
        url = target.url.rstrip("/") + ("/stories" if kind == "stories" else "")
    else:
        url = target.url.rstrip("/") + "/tweets"
    data = await _extract(["gallery-dl", "-J", "--range", "1-15", url], 35)
    return _gallery_items(data, target, kind)


async def public_profile(target: Target) -> dict[str, str]:
    """Prefer structured public profile fields; OG summaries are not a reliable bio."""
    await asyncio.to_thread(validate_dns, target.url)
    if target.platform in {"instagram", "twitter", "tiktok"}:
        endpoint = (target.url.rstrip("/") + "/info" if target.platform != "tiktok"
                    else target.url.rstrip("/") + "/avatar")
        try:
            data = await _extract(["gallery-dl", "-J", "--range", "1-1", endpoint], 25)
            if isinstance(data, list):
                for message in data:
                    if not isinstance(message, list) or len(message) < 3 or not isinstance(message[-1], dict):
                        continue
                    meta = message[-1]
                    if target.platform == "instagram":
                        picture = meta.get("hd_profile_pic_url_info")
                        result = {
                            "name": meta.get("full_name") or "",
                            "bio": meta.get("biography") or "",
                            "avatar": (picture.get("url") if isinstance(picture, dict) else "")
                            or meta.get("profile_pic_url") or "",
                        }
                        if "biography" not in meta:
                            result.pop("bio")
                    elif target.platform == "twitter":
                        result = {"name": meta.get("nick") or "",
                                  "bio": meta.get("description") or "",
                                  "avatar": meta.get("profile_image") or ""}
                        if "description" not in meta:
                            result.pop("bio")
                    else:
                        user = meta.get("user") if isinstance(meta.get("user"), dict) else meta
                        result = {"name": user.get("nickname") or "",
                                  "bio": user.get("signature") or "",
                                  "avatar": user.get("avatarLarger") or user.get("avatarMedium") or ""}
                        if "signature" not in user:
                            result.pop("bio")
                    if any(result.values()):
                        return _stable_profile(result)
        except (ValueError, TimeoutError, OSError, json.JSONDecodeError):
            pass
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        response = await client.get(target.url, headers={"User-Agent": "facebookexternalhit/1.1"})
        response.raise_for_status()
    parser = _Meta()
    parser.feed(response.text[:500_000])
    data = parser.data
    return _stable_profile({"name": data.get("og:title", ""), "avatar": data.get("og:image", "")})


def _stable_profile(data: dict[str, object]) -> dict[str, str]:
    result = {key: str(data.get(key) or "")[:500] for key in ("name", "bio", "avatar")
              if key in data and (data.get(key) or key == "bio")}
    if result.get("avatar"):
        parts = urlsplit(result["avatar"])
        result["avatar"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return result


class FollowService:
    def __init__(self, store: Store, bot: Bot, private_chats_only: bool = False):
        self.store, self.bot = store, bot
        self.private_chats_only = private_chats_only
        self.db = FollowStore(store.db)
        self.running = False

    async def loop(self) -> None:
        self.running = True
        while self.running:
            row = self.db.db.execute("SELECT * FROM follows WHERE next_check<=? ORDER BY next_check LIMIT 1",
                                     (time.time(),)).fetchone()
            if row:
                chat_type = "private" if row["chat_id"] == row["user_id"] else "group"
                if not allowed(row["user_id"], row["chat_id"], chat_type,
                               self.store.allowlist(), self.private_chats_only, self.store.groups()):
                    self.db.remove(row["id"], row["user_id"])
                    continue
                try:
                    await self.check(row)
                except (ValueError, TimeoutError, httpx.HTTPError, OSError, json.JSONDecodeError):
                    await self._failed(row)
                except Exception:
                    # Keep the polling task alive; failed checks are retried later.
                    await self._failed(row)
            else:
                await asyncio.sleep(5)

    async def _failed(self, row) -> None:
        failures = row["failures"] + 1
        self.db.db.execute("UPDATE follows SET failures=?,next_check=? WHERE id=?",
                           (failures, time.time() + INTERVAL, row["id"]))
        if failures == 3:
            try:
                await self.bot.send_message(row["chat_id"],
                    f"⚠️ Follow @{html.escape(row['handle'])}: public updates could not be checked. "
                    "Anonymous access may be blocked. Monitoring will retry automatically.")
            except Exception:
                pass

    async def check(self, row) -> None:
        target = Target(row["platform"], row["handle"], row["url"])
        selected = set(json.loads(row["features"]))
        baselines = {r[0] for r in self.db.db.execute(
            "SELECT category FROM follow_baselines WHERE follow_id=?", (row["id"],))}
        batches: list[tuple[str, str, str]] = []
        main_ok = False
        story_ok = False
        if selected & {"posts", "reels", "tweets", "videos"}:
            batches.extend(await public_items(target))
            main_ok = True
        shorts_ok = False
        if "shorts" in selected:
            try:
                batches.extend(await public_items(target, "shorts"))
                shorts_ok = True
            except (ValueError, TimeoutError, OSError, json.JSONDecodeError):
                pass
        if "stories" in selected:
            try:
                batches.extend(await public_items(target, "stories"))
                story_ok = True
            except (ValueError, TimeoutError, OSError, json.JSONDecodeError):
                pass
        profile = {}
        if selected & {"bio", "name", "avatar"}:
            try:
                profile = await public_profile(target)
            except (ValueError, TimeoutError, httpx.HTTPError):
                pass
        if not main_ok and not shorts_ok and not story_ok and not profile:
            raise ValueError("no public data")
        old_profile = json.loads(row["profile_data"])
        for event_kind, url, title in reversed(batches):
            if event_kind not in selected:
                continue
            key = f"{event_kind}:{url}"
            if self.db.db.execute("SELECT 1 FROM follow_seen WHERE follow_id=? AND event_key=?",
                                  (row["id"], key)).fetchone():
                continue
            if event_kind in baselines and row["mode"] == "media":
                self.db.db.execute("BEGIN IMMEDIATE")
                try:
                    await self.deliver(row, target, event_kind, url, title)
                    self.db.db.execute("INSERT OR IGNORE INTO follow_seen VALUES (?,?)",
                                       (row["id"], key))
                    self.db.db.execute("COMMIT")
                except BaseException:
                    self.db.db.execute("ROLLBACK")
                    raise
                continue
            if event_kind in baselines:
                await self.deliver(row, target, event_kind, url, title)
            self.db.db.execute("INSERT OR IGNORE INTO follow_seen VALUES (?,?)", (row["id"], key))
        for key in selected & {"bio", "name", "avatar"}:
            if key in baselines and key in profile and key in old_profile and profile[key] != old_profile[key]:
                value = "Avatar updated" if key == "avatar" else html.escape(profile[key][:500])
                await self.bot.send_message(row["chat_id"],
                    f"👀 <b>{target.platform.title()} · @{html.escape(target.handle)}</b>\n"
                    f"{LABELS[key]}\n\n{value}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔗 Profile", url=target.url)]]))
        categories = []
        if main_ok:
            categories.extend(selected & {"posts", "reels", "tweets", "videos"})
        if story_ok:
            categories.append("stories")
        if shorts_ok:
            categories.append("shorts")
        categories.extend(key for key in selected & {"bio", "name", "avatar"} if key in profile)
        self.db.db.executemany("INSERT OR IGNORE INTO follow_baselines VALUES (?,?)",
                               ((row["id"], key) for key in categories))
        old_profile.update(profile)
        self.db.db.execute("UPDATE follows SET baseline=1,profile_data=?,next_check=?,failures=0 "
                           "WHERE id=?", (json.dumps(old_profile), time.time() + INTERVAL, row["id"]))
        self.db.db.execute("DELETE FROM follow_seen WHERE follow_id=? AND rowid NOT IN "
                           "(SELECT rowid FROM follow_seen WHERE follow_id=? ORDER BY rowid DESC LIMIT 100)",
                           (row["id"], row["id"]))

    async def deliver(self, row, target: Target, kind: str, url: str, title: str) -> None:
        if row["mode"] == "media":
            self.store.create_job(row["user_id"], row["chat_id"], url, "auto",
                                  {"kind": "post", "source_url": url,
                                   "profile_url": target.url, "follow_event": kind})
            return
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Link", url=url),
             InlineKeyboardButton(text="⬇️ Download", callback_data=f"fd:{row['id']}:{secrets.token_urlsafe(5)}")]])
        # A short token maps to a validated URL; callback data never contains URLs.
        token = markup.inline_keyboard[0][1].callback_data.split(":")[-1]
        self.db.db.execute("INSERT INTO follow_downloads VALUES (?,?,?,?,?)",
                           (token, row["id"], row["user_id"], url, time.time() + 86400))
        await self.bot.send_message(row["chat_id"],
            f"👀 <b>{target.platform.title()} · @{html.escape(target.handle)}</b>\n"
            f"New {html.escape(kind)}{': ' + html.escape(title[:200]) if title else ''}",
            reply_markup=markup)
