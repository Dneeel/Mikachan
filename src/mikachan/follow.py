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
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .security import allowed, clean_url, instagram_cookie_args, source, validate_dns
from .public_social import public_statuses, public_x_media
from .store import Store

INTERVAL = 15 * 60
MAX_FOLLOWS = 25
FEATURES = {
    "instagram": ("posts", "reels", "stories", "bio", "name", "avatar"),
    "twitter": ("tweets", "bio", "name", "avatar"),
    "youtube": ("videos", "shorts", "live", "bio", "name", "avatar"),
    "tiktok": ("posts", "stories", "live", "bio", "name", "avatar"),
}
SOCIAL_PLATFORMS = frozenset(FEATURES)
LABELS = {
    "posts": "Posts", "reels": "Reels", "stories": "Stories (best effort)",
    "tweets": "Tweets", "videos": "Videos", "shorts": "Shorts", "live": "Live started", "bio": "Bio changes",
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
    if platform == "twitter" and len(parts) >= 3 and parts[1] == "status":
        return profile_target(f"https://x.com/{parts[0]}")
    if platform == "tiktok" and len(parts) >= 2 and parts[0].startswith("@"):
        return profile_target(f"https://www.tiktok.com/{parts[0]}")
    if platform == "instagram" and len(parts) >= 2 and parts[0] == "stories":
        return profile_target(f"https://www.instagram.com/{parts[1]}/")
    candidate = meta.get("uploader_url") or meta.get("channel_url") or meta.get("profile_url")
    if isinstance(candidate, str):
        found = profile_target(candidate)
        if found:
            return found
    if platform == "instagram":
        candidate = meta.get("username") or meta.get("uploader_id") or meta.get("uploader")
        if (isinstance(candidate, str) and re.fullmatch(r"[\w.]{1,30}", candidate)
                and not candidate.isdigit()):
            return profile_target(f"https://www.instagram.com/{candidate}/")
    return None


async def resolve_creator_from_post(
    url: str, meta: dict[str, object] | None = None,
) -> Target | None:
    """Resolve a public post's creator without downloading its media."""
    url = clean_url(url)
    target = creator_from_post(url, meta)
    if target:
        return target
    platform = source(url)
    if platform not in {"instagram", "youtube"}:
        return None
    await asyncio.to_thread(validate_dns, url)
    if platform == "instagram":
        try:
            data = await _extract(["gallery-dl", "-J", "--range", "1-1", url], 20)
            if isinstance(data, list):
                for message in data:
                    if (isinstance(message, list) and len(message) >= 2
                            and isinstance(message[-1], dict)):
                        target = creator_from_post(url, message[-1])
                        if target:
                            return target
        except (ValueError, TimeoutError, OSError, json.JSONDecodeError):
            pass
    try:
        data = await _extract(["yt-dlp", "--no-playlist", "--skip-download",
                               "--dump-single-json", "--no-warnings",
                               "--socket-timeout", "10", url], 25)
        if isinstance(data, dict):
            target = creator_from_post(url, data)
            if target:
                return target
            channel_id = data.get("channel_id")
            if (platform == "youtube" and isinstance(channel_id, str)
                    and re.fullmatch(r"UC[\w-]{22}", channel_id)):
                return profile_target(f"https://www.youtube.com/channel/{channel_id}")
    except (ValueError, TimeoutError, OSError, json.JSONDecodeError):
        pass
    if platform == "youtube":
        try:
            await asyncio.to_thread(validate_dns, "https://www.youtube.com/oembed")
            async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                response = await client.get("https://www.youtube.com/oembed",
                                            params={"url": url, "format": "json"})
                response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("author_url"), str):
                return profile_target(payload["author_url"])
        except (ValueError, TimeoutError, httpx.HTTPError):
            pass
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
        CREATE TABLE IF NOT EXISTS profile_panels (
          id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
          message_id INTEGER, platform TEXT NOT NULL, handle TEXT NOT NULL,
          url TEXT NOT NULL, expires REAL NOT NULL);
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
        CREATE TABLE IF NOT EXISTS follow_media_jobs (
          follow_id TEXT NOT NULL, event_key TEXT NOT NULL,
          url TEXT NOT NULL, job_id TEXT NOT NULL, attempts INTEGER NOT NULL,
          next_retry REAL NOT NULL, PRIMARY KEY(follow_id,event_key));
        CREATE INDEX IF NOT EXISTS follow_media_retry ON follow_media_jobs(next_retry);
        CREATE TABLE IF NOT EXISTS follow_baselines (
          follow_id TEXT NOT NULL, category TEXT NOT NULL,
          PRIMARY KEY(follow_id,category));
        CREATE TABLE IF NOT EXISTS follow_health (
          follow_id TEXT NOT NULL, category TEXT NOT NULL,
          failures INTEGER NOT NULL DEFAULT 0, warned INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(follow_id,category));
        CREATE TABLE IF NOT EXISTS follow_start (
          follow_id TEXT PRIMARY KEY, started REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS follow_alerts (
          follow_id TEXT NOT NULL, category TEXT NOT NULL, warned_at REAL NOT NULL,
          PRIMARY KEY(follow_id,category));
        CREATE INDEX IF NOT EXISTS follows_due ON follows(next_check);
        CREATE TABLE IF NOT EXISTS follow_downloads (
          token TEXT PRIMARY KEY, follow_id TEXT NOT NULL, user_id INTEGER NOT NULL,
          url TEXT NOT NULL, expires REAL NOT NULL,
          kind TEXT NOT NULL DEFAULT 'post', value TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS follow_notifications (
          token TEXT PRIMARY KEY, follow_id TEXT NOT NULL, user_id INTEGER NOT NULL,
          chat_id INTEGER NOT NULL, message_id INTEGER, kind TEXT NOT NULL,
          expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS follow_menus (
          chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
          expires REAL NOT NULL, PRIMARY KEY(chat_id,message_id));
        CREATE TABLE IF NOT EXISTS follow_source_errors (
          follow_id TEXT NOT NULL, category TEXT NOT NULL, reason TEXT NOT NULL,
          checked_at REAL NOT NULL, PRIMARY KEY(follow_id, category));
        """)
        # Upgrade an existing SQLite database without losing follows or buttons.
        columns = {r[1] for r in db.execute("PRAGMA table_info(follow_downloads)")}
        if "kind" not in columns:
            db.execute("ALTER TABLE follow_downloads ADD COLUMN kind TEXT NOT NULL DEFAULT 'post'")
        if "value" not in columns:
            db.execute("ALTER TABLE follow_downloads ADD COLUMN value TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE follows SET mode='notice' WHERE mode='media'")

    def session(self, user: int, chat: int, target: Target) -> str:
        self.db.execute("DELETE FROM follow_sessions WHERE expires<?", (time.time(),))
        token = secrets.token_urlsafe(8)
        # Follow defaults to all *implemented* events. Unsupported provider access
        # is shown as unavailable, never quietly presented as monitored.
        defaults = list(FEATURES[target.platform])
        self.db.execute("INSERT INTO follow_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (token, user, chat, None, target.platform, target.handle,
                         target.url, "confirm", json.dumps(defaults), "notice", "dm", time.time() + 1800))
        return token

    def edit_session(self, user: int, chat: int, follow_id: str) -> str | None:
        row = self.db.execute("SELECT * FROM follows WHERE id=? AND user_id=?",
                              (follow_id, user)).fetchone()
        if row is None:
            return None
        token = secrets.token_urlsafe(8)
        self.db.execute("INSERT INTO follow_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (token, user, chat, None, row["platform"], row["handle"],
                         row["url"], f"edit:{follow_id}", row["features"],
                         row["mode"], "keep", time.time() + 1800))
        return token

    def profile_panel(self, user: int, chat: int, target: Target) -> str:
        self.db.execute("DELETE FROM profile_panels WHERE expires<?", (time.time(),))
        token = secrets.token_urlsafe(8)
        self.db.execute("INSERT INTO profile_panels VALUES (?,?,?,?,?,?,?,?)",
                        (token, user, chat, None, target.platform, target.handle,
                         target.url, time.time() + 7 * 86400))
        return token

    def bind_profile_panel(self, token: str, message: int) -> None:
        self.db.execute("UPDATE profile_panels SET message_id=? WHERE id=?", (message, token))

    def get_profile_panel(self, token: str, user: int, chat: int, message: int):
        return self.db.execute("SELECT * FROM profile_panels WHERE id=? AND user_id=? "
                               "AND chat_id=? AND message_id=? AND expires>?",
                               (token, user, chat, message, time.time())).fetchone()

    def bind(self, token: str, message: int) -> None:
        self.db.execute("UPDATE follow_sessions SET message_id=? WHERE id=?", (message, token))

    def get_session(self, token: str, user: int, chat: int, message: int):
        return self.db.execute("SELECT * FROM follow_sessions WHERE id=? AND user_id=? "
                               "AND chat_id=? AND message_id=? AND expires>?",
                               (token, user, chat, message, time.time())).fetchone()

    def save(self, session) -> str:
        target = session["url"]
        if session["stage"].startswith("edit:"):
            follow_id = session["stage"].split(":", 1)[1]
            updated = self.db.execute(
                "UPDATE follows SET features=?,mode=? WHERE id=? AND user_id=? AND url=? "
                "RETURNING id",
                (session["features"], "notice", follow_id,
                 session["user_id"], target)).fetchone()
            if not updated:
                raise ValueError("Follow no longer exists")
            self.db.execute("DELETE FROM follow_sessions WHERE id=?", (session["id"],))
            return follow_id
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
                         session["handle"], target, session["features"], "notice", time.time()))
        if not existing:
            self.db.execute("INSERT INTO follow_start VALUES (?,?)", (token, time.time()))
        self.db.execute("DELETE FROM follow_sessions WHERE id=?", (session["id"],))
        return token

    def remove(self, token: str, user: int) -> bool:
        row = self.db.execute("DELETE FROM follows WHERE id=? AND user_id=? RETURNING id",
                              (token, user)).fetchone()
        if row:
            self.db.execute("DELETE FROM follow_seen WHERE follow_id=?", (token,))
            self.db.execute("DELETE FROM follow_media_jobs WHERE follow_id=?", (token,))
            self.db.execute("DELETE FROM follow_baselines WHERE follow_id=?", (token,))
            self.db.execute("DELETE FROM follow_health WHERE follow_id=?", (token,))
            self.db.execute("DELETE FROM follow_start WHERE follow_id=?", (token,))
            self.db.execute("DELETE FROM follow_alerts WHERE follow_id=?", (token,))
            self.db.execute("DELETE FROM follow_source_errors WHERE follow_id=?", (token,))
            self.db.execute("DELETE FROM follow_downloads WHERE follow_id=?", (token,))
            self.db.execute("DELETE FROM follow_notifications WHERE follow_id=?", (token,))
        return bool(row)

    def notification(self, token: str, user: int, chat: int, message: int):
        return self.db.execute(
            "SELECT n.*,f.platform,f.handle,f.url FROM follow_notifications n "
            "LEFT JOIN follows f ON f.id=n.follow_id AND f.user_id=n.user_id "
            "WHERE n.token=? AND n.user_id=? AND n.chat_id=? AND n.message_id=? "
            "AND n.expires>?",
            (token, user, chat, message, time.time())).fetchone()

    def bind_menu(self, user: int, chat: int, message: int) -> None:
        self.db.execute("INSERT INTO follow_menus VALUES (?,?,?,?) "
                        "ON CONFLICT(chat_id,message_id) DO UPDATE SET "
                        "user_id=excluded.user_id,expires=excluded.expires",
                        (chat, message, user, time.time() + 86400))

    def menu_owned(self, user: int, chat: int, message: int) -> bool:
        return self.db.execute("SELECT 1 FROM follow_menus WHERE user_id=? AND chat_id=? "
                               "AND message_id=? AND expires>?",
                               (user, chat, message, time.time())).fetchone() is not None


def confirm_panel(target: Target, token: str) -> tuple[str, InlineKeyboardMarkup]:
    notes = {
        "instagram": "Public posts, reels and best-effort stories. Live and handle-change alerts are unavailable.",
        "twitter": "Public tweets and visible profile changes. X may block anonymous access.",
        "youtube": "Videos, Shorts, live detection and public profile changes (best effort).",
        "tiktok": "Public posts, stories and live detection (best effort).",
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
    # All new events are notifications with two controls. Auto-download is
    # intentionally retired: the operator explicitly requests each payload.
    editing = session["stage"].startswith("edit:")
    if not editing and session["chat_id"] != session["user_id"]:
        rows.append([InlineKeyboardButton(text=f"{'✅' if session['destination'] == 'dm' else '⬜'} Private chat",
                                        callback_data=f"f:{token}:d:dm"),
                     InlineKeyboardButton(text=f"{'✅' if session['destination'] == 'here' else '⬜'} This group",
                                        callback_data=f"f:{token}:d:here")])
    rows.append([InlineKeyboardButton(text="💾 Save changes" if editing else "💾 Save Follow",
                                    callback_data=f"f:{token}:save")])
    rows.append([InlineKeyboardButton(text="Cancel", callback_data=f"f:{token}:no")])
    body = (f"👀 <b>Follow @{html.escape(session['handle'])}</b>\n"
            "Choose monitored events and delivery. All supported events are selected by default.\n\n"
            "Stories, lives and profile details are best effort: some platforms block "
            "public requests. Handle changes are not supported without stable account IDs.\n"
            "Every event sends two buttons: Get it and View original (on demand)."
            + ("\nYour existing notification destination stays the same." if editing else ""))
    return body, InlineKeyboardMarkup(inline_keyboard=rows)


async def _extract(command: list[str], timeout: int) -> object:
    executable = shutil.which(command[0]) or str(Path(sys.executable).with_name(command[0]))
    if not Path(executable).is_file():
        raise ValueError("public source extractor is not installed")
    command = [executable, *command[1:]]
    if Path(executable).name == "gallery-dl":
        command = [command[0], "--config-ignore", "--cache-file", ":memory:",
                   *instagram_cookie_args(command[-1]), *command[1:]]
    elif Path(executable).name == "yt-dlp":
        command = [command[0], "--ignore-config", *instagram_cookie_args(command[-1]),
                   *command[1:]]
    network_env = {key: value for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
                   if (value := os.environ.get(key))}
    network_env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    proc = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True, env=network_env)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        os.killpg(proc.pid, signal.SIGTERM)
        await proc.wait()
        raise
    if proc.returncode:
        # Only emit a small sanitized classification, never raw stderr URLs,
        # authentication headers, cookies, or remote profile values.
        diagnostic = stderr[:8192].lower()
        if b"403" in diagnostic or b"forbidden" in diagnostic:
            raise ValueError("provider refused access (HTTP 403)")
        if b"429" in diagnostic or b"rate limit" in diagnostic:
            raise ValueError("provider rate limit (HTTP 429)")
        if b"login" in diagnostic or b"cookies" in diagnostic or b"sign in" in diagnostic:
            raise ValueError("provider needs an authorized login session")
        raise ValueError("public extractor failed (unknown provider error)")
    if len(stdout) > 3_000_000:
        raise ValueError("public source metadata exceeded the size limit")
    return json.loads(stdout)


def _gallery_items(data: object, target: Target, kind: str) -> list[tuple[str, str, str]]:
    if not isinstance(data, list):
        raise ValueError("invalid gallery response")
    if any(isinstance(message, list) and message and message[0] == -1 for message in data):
        raise ValueError("public gallery source returned an error")
    found: dict[str, tuple[str, str, str]] = {}
    for message in data:
        if not isinstance(message, list) or len(message) < 2 or not isinstance(message[-1], dict):
            continue
        meta = message[-1]
        if target.platform == "instagram":
            item_id = ((meta.get("story_media_id") or meta.get("id") or meta.get("post_id"))
                       if kind == "stories" else meta.get("post_shortcode"))
            event_kind = "stories" if kind == "stories" else (
                "reels" if kind == "reels" or meta.get("type") == "reel" else "posts")
            url = (f"https://www.instagram.com/stories/{target.handle}/{item_id}/" if kind == "stories"
                   else f"https://www.instagram.com/{'reel' if event_kind == 'reels' else 'p'}/{item_id}/")
        elif target.platform == "twitter":
            item_id = meta.get("tweet_id")
            event_kind = "tweets"
            url = f"https://x.com/{target.handle}/status/{item_id}"
        else:
            item_id = meta.get("id")
            event_kind = "stories" if kind == "stories" else "posts"
            # The public story listing yields post IDs; the individual extractor
            # accepts /video/ID, not the unsupported /story/ID path.
            url = f"https://www.tiktok.com/@{target.handle}/video/{item_id}"
        if not item_id or meta.get("type") == "avatar":
            continue
        try:
            url = clean_url(url)
        except ValueError:
            continue
        found[str(item_id)] = (event_kind, url,
                               str(meta.get("description") or meta.get("content")
                                   or meta.get("title") or "")[:300])
    return list(found.values())[:15]


async def _youtube_feed_items(target: Target) -> list[tuple[str, str, str]]:
    """Read the public channel feed before invoking a slower channel extractor."""
    parts = [part for part in urlsplit(target.url).path.split("/") if part]
    channel_id = parts[1] if len(parts) == 2 and parts[0] == "channel" else ""
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        if not re.fullmatch(r"UC[\w-]{22}", channel_id):
            await asyncio.to_thread(validate_dns, target.url)
            page = await client.get(target.url)
            page.raise_for_status()
            if len(page.content) > 2_000_000:
                raise ValueError("YouTube channel page is too large")
            match = re.search(
                r'<meta\s+itemprop="channelId"\s+content="(UC[\w-]{22})"', page.text
            ) or re.search(r'"externalId":"(UC[\w-]{22})"', page.text)
            if not match:
                raise ValueError("YouTube channel ID is unavailable")
            channel_id = match[1]
        endpoint = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        await asyncio.to_thread(validate_dns, endpoint)
        response = await client.get(endpoint)
        response.raise_for_status()
    if len(response.content) > 500_000:
        raise ValueError("YouTube feed is too large")
    root = ET.fromstring(response.content)
    namespace = {"atom": "http://www.w3.org/2005/Atom",
                 "yt": "http://www.youtube.com/xml/schemas/2015"}
    feed_id = root.findtext("yt:channelId", namespaces=namespace)
    if feed_id != channel_id:
        raise ValueError("YouTube feed identity mismatch")
    items = []
    for entry in root.findall("atom:entry", namespace)[:15]:
        video_id = entry.findtext("yt:videoId", namespaces=namespace)
        if re.fullmatch(r"[\w-]{11}", video_id or ""):
            items.append(("videos", f"https://www.youtube.com/watch?v={video_id}",
                          (entry.findtext("atom:title", default="Video", namespaces=namespace)
                           or "Video")[:300]))
    return items


async def public_items(target: Target, kind: str = "posts") -> list[tuple[str, str, str]]:
    await asyncio.to_thread(validate_dns, target.url)
    primary_failed = False
    if target.platform in {"twitter", "tiktok"} and kind == "posts":
        try:
            items = await public_statuses(target.platform, target.handle)
            if items:
                return items
        except (ValueError, TimeoutError, httpx.HTTPError):
            primary_failed = True
    if kind == "live" and target.platform in {"youtube", "tiktok"}:
        if target.platform == "youtube":
            endpoint = target.url.rstrip("/") + "/streams"
            data = await _extract([
                "yt-dlp", "--flat-playlist", "--playlist-end", "12",
                "--dump-single-json", "--skip-download", "--no-warnings", endpoint,
            ], 35)
            if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
                raise ValueError("live source returned no valid listing")
            items = []
            entries = [e for e in data["entries"][:12] if isinstance(e, dict)]
            if entries and all(e.get("live_status") is None and e.get("is_live") is None
                               for e in entries):
                raise ValueError("live source does not expose trustworthy live status")
            for entry in entries:
                ident = str(entry.get("id") or "")
                if (re.fullmatch(r"[\w-]{11}", ident) and
                        (entry.get("live_status") == "is_live" or entry.get("is_live") is True)):
                    items.append(("live", f"https://www.youtube.com/watch?v={ident}",
                                  str(entry.get("title") or "Live started")[:300]))
            return items
        data = await _extract([
            "yt-dlp", "--no-playlist", "--skip-download", "--dump-single-json",
            "--no-warnings", target.url.rstrip("/") + "/live",
        ], 25)
        if not isinstance(data, dict) or not (data.get("live_status") == "is_live"
                                              or data.get("is_live") is True):
            return []
        creator = str(data.get("uploader_id") or data.get("channel_id") or "").lstrip("@")
        if not creator or creator.casefold() != target.handle.casefold():
            raise ValueError("live stream owner identity mismatch")
        ident = str(data.get("id") or "")
        if not re.fullmatch(r"\d{10,22}", ident):
            raise ValueError("live stream ID is unavailable")
        return [("live", f"https://www.tiktok.com/@{target.handle}/live?room_id={ident}",
                 str(data.get("title") or "Live started")[:300])]
    if target.platform == "youtube":
        if kind != "shorts":
            try:
                return await _youtube_feed_items(target)
            except (ValueError, TimeoutError, OSError, ET.ParseError, httpx.HTTPError):
                pass
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
               else target.url.rstrip("/") + ("/reels/" if kind == "reels" else "/posts/"))
    elif target.platform == "tiktok":
        url = target.url.rstrip("/") + ("/stories" if kind == "stories" else "")
    else:
        url = target.url.rstrip("/") + "/tweets"
    command = ["gallery-dl", "-J", "--range", "1-15"]
    if target.platform == "twitter":
        # gallery-dl omits text-only tweets unless this option is enabled.
        command.extend(["-o", "extractor.twitter.text-tweets=true"])
    try:
        data = await _extract([*command, url], 35)
        items = _gallery_items(data, target, kind)
        if target.platform == "twitter" and kind == "posts" and primary_failed and not items:
            raise ValueError("public X timeline is unavailable")
        return items
    except (ValueError, TimeoutError, OSError, json.JSONDecodeError):
        if target.platform != "tiktok" or kind == "stories":
            raise
        # Some public TikTok timelines are unavailable to gallery-dl while
        # yt-dlp's user extractor can still return their post IDs.
        data = await _extract([
            "yt-dlp", "--flat-playlist", "--playlist-end", "15",
            "--dump-single-json", "--skip-download", "--no-warnings", target.url,
        ], 35)
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise ValueError("public TikTok timeline unavailable")
        fallback_items: list[tuple[str, str, str]] = []
        for entry in data["entries"][:15]:
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("id") or "")
            if re.fullmatch(r"\d{10,22}", item_id):
                fallback_items.append((
                    "posts", f"https://www.tiktok.com/@{target.handle}/video/{item_id}",
                    str(entry.get("title") or "")[:300],
                ))
        return fallback_items


async def public_profile(target: Target, *, display_avatar: bool = False) -> dict[str, str]:
    """Use structured public fields; OG summaries are not reliable change signals."""
    await asyncio.to_thread(validate_dns, target.url)
    if target.platform in {"instagram", "twitter", "tiktok"}:
        endpoint = (target.url.rstrip("/") + "/info" if target.platform != "tiktok"
                    else target.url.rstrip("/") + "/avatar")
        try:
            data = await _extract(["gallery-dl", "-J", "--range", "1-1", endpoint], 25)
            if isinstance(data, list):
                for message in data:
                    if not isinstance(message, list) or len(message) < 2 or not isinstance(message[-1], dict):
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
                        user: dict = meta["user"] if isinstance(meta.get("user"), dict) else meta
                        result = {"name": user.get("nickname") or "",
                                  "bio": user.get("signature") or ""}
                        # Signed CDN URLs rotate without a profile edit. Only a stable
                        # provider avatar identifier may trigger a change alert.
                        if display_avatar:
                            result["avatar"] = user.get("avatarLarger") or user.get("avatarMedium") or ""
                        elif isinstance(user.get("avatarUri"), str) and user["avatarUri"]:
                            result["avatar"] = user["avatarUri"]
                        elif isinstance(meta.get("file_id"), str) and re.fullmatch(
                                r"[A-Za-z0-9_-]{8,128}", meta["file_id"]):
                            # gallery-dl derives this from the avatar object filename,
                            # omitting TikTok's rotating CDN transform suffix.
                            result["avatar"] = meta["file_id"]
                        if "signature" not in user:
                            result.pop("bio")
                    if any(result.values()):
                        return _stable_profile(result, display_avatar=display_avatar)
        except (ValueError, TimeoutError, OSError, json.JSONDecodeError):
            pass
    if target.platform == "youtube":
        data = await _extract([
            "yt-dlp", "--flat-playlist", "--playlist-end", "1",
            "--dump-single-json", "--skip-download", "--no-warnings", target.url,
        ], 25)
        if isinstance(data, dict):
            name = str(data.get("channel") or data.get("uploader") or "")
            bio = str(data.get("description") or "")
            thumbnails = data.get("thumbnails")
            avatar = str(data.get("channel_thumbnail") or "")
            if not avatar and isinstance(thumbnails, list):
                pictures = [t.get("url") for t in thumbnails if isinstance(t, dict)
                            and isinstance(t.get("url"), str)]
                avatar = pictures[-1] if pictures else ""
            result = {"name": name} if name else {}
            if "description" in data:
                result["bio"] = bio
            if avatar.startswith("https://"):
                result["avatar"] = avatar
            if result:
                return _stable_profile(result, display_avatar=display_avatar)
    raise ValueError("structured public profile data unavailable")


def _stable_profile(data: dict[str, object], *, display_avatar: bool = False) -> dict[str, str]:
    result = {key: str(data.get(key) or "")[:500] for key in ("name", "bio", "avatar")
              if key in data and (data.get(key) or key == "bio")}
    if result.get("avatar") and not display_avatar:
        parts = urlsplit(result["avatar"])
        result["avatar"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return result


def _post_timestamp(url: str) -> float | None:
    """Read the published time encoded in X and TikTok public post IDs."""
    parts = [part for part in urlsplit(url).path.split("/") if part]
    if len(parts) < 3 or not re.fullmatch(r"\d{10,22}", parts[2]):
        return None
    ident = int(parts[2])
    if source(url) == "twitter" and parts[1] == "status":
        return ((ident >> 22) + 1288834974657) / 1000
    if source(url) == "tiktok" and parts[1] == "video":
        return float(ident >> 32)
    return None


class FollowService:
    def __init__(self, store: Store, bot: Bot, private_chats_only: bool = False):
        self.store, self.bot = store, bot
        self.private_chats_only = private_chats_only
        self.db = FollowStore(store.db)
        self.running = False
        self.check_lock = asyncio.Lock()

    def _enabled(self, chat: int) -> bool:
        access = self.store.access_registry
        if access is not None and not access.module_enabled("follow"):
            return False
        if chat < 0:
            try:
                row = self.db.db.execute("SELECT value FROM group_settings WHERE chat_id=? "
                                         "AND name='module_follow'", (chat,)).fetchone()
            except sqlite3.OperationalError:
                row = None
            if row and row[0] == "0":
                return False
        return True

    async def loop(self) -> None:
        self.running = True
        while self.running:
            if not self._enabled(0):
                await asyncio.sleep(10)
                continue
            self.retry_failed_media()
            row = self.db.db.execute("SELECT * FROM follows WHERE next_check<=? ORDER BY next_check LIMIT 1",
                                     (time.time(),)).fetchone()
            if row:
                if not self._enabled(int(row["chat_id"])):
                    self.db.db.execute("UPDATE follows SET next_check=? WHERE id=?",
                                       (time.time() + INTERVAL, row["id"]))
                    continue
                chat_type = "private" if row["chat_id"] == row["user_id"] else "group"
                if not (self.store.can_access(row["user_id"], row["chat_id"], chat_type)
                        if self.store.access_registry is not None else allowed(
                            row["user_id"], row["chat_id"], chat_type,
                            self.store.allowlist(), self.private_chats_only,
                            self.store.groups())):
                    # Access changes are reversible. Do not destroy the owner's
                    # subscription or baseline on a temporary permission failure.
                    self.db.db.execute("UPDATE follows SET next_check=? WHERE id=?",
                                       (time.time() + INTERVAL, row["id"]))
                    await asyncio.sleep(0)
                    continue
                try:
                    async with self.check_lock:
                        await self.check(row)
                except (ValueError, TimeoutError, httpx.HTTPError, OSError, json.JSONDecodeError):
                    await self._failed(row)
                except Exception:
                    # Keep the polling task alive; failed checks are retried later.
                    await self._failed(row)
            else:
                await asyncio.sleep(5)

    def retry_failed_media(self) -> bool:
        """Retry proven pre-delivery failures; uncertain deliveries never duplicate."""
        row = self.db.db.execute("SELECT m.*,f.user_id,f.chat_id,f.platform,f.url AS profile_url "
            "FROM follow_media_jobs m JOIN follows f ON f.id=m.follow_id "
            "JOIN jobs j ON j.id=m.job_id WHERE j.state='FAILED' AND m.attempts<3 "
            "AND m.next_retry<=? ORDER BY m.next_retry LIMIT 1", (time.time(),)).fetchone()
        if not row:
            return False
        chat_type = "private" if row["chat_id"] == row["user_id"] else "group"
        if not self._enabled(int(row["chat_id"])) or (
                self.store.access_registry is not None and not self.store.can_access(
                    row["user_id"], row["chat_id"], chat_type)):
            self.db.db.execute("UPDATE follow_media_jobs SET next_retry=? "
                "WHERE follow_id=? AND event_key=?", (time.time() + INTERVAL,
                 row["follow_id"], row["event_key"]))
            return False
        self.db.db.execute("BEGIN IMMEDIATE")
        try:
            current = self.db.db.execute("SELECT j.state FROM follow_media_jobs m "
                "JOIN jobs j ON j.id=m.job_id WHERE m.follow_id=? AND m.event_key=?",
                (row["follow_id"], row["event_key"])).fetchone()
            if not current or current[0] != "FAILED":
                self.db.db.execute("COMMIT")
                return False
            kind = str(row["event_key"]).split(":", 1)[0]
            jid = self.store.create_job(row["user_id"], row["chat_id"], row["url"], "auto",
                {"kind": "post", "source_url": row["url"],
                 "profile_url": row["profile_url"], "follow_event": kind})
            attempts = int(row["attempts"]) + 1
            self.db.db.execute("UPDATE follow_media_jobs SET job_id=?,attempts=?,next_retry=? "
                "WHERE follow_id=? AND event_key=?", (jid, attempts,
                time.time() + attempts * 900, row["follow_id"], row["event_key"]))
            self.db.db.execute("COMMIT")
            return True
        except BaseException:
            self.db.db.execute("ROLLBACK")
            raise

    async def _failed(self, row) -> None:
        failures = row["failures"] + 1
        self.db.db.execute("UPDATE follows SET failures=?,next_check=? WHERE id=?",
                           (failures, time.time() + INTERVAL, row["id"]))
        if self._alert_due(row["id"], "all"):
            try:
                await self.bot.send_message(row["chat_id"],
                    f"⚠️ Follow @{html.escape(row['handle'])}: no selected public source "
                    "could be checked. This follow is saved, but alerts are not active "
                    "until a source responds. Use /following → Check now for status. "
                    "Monitoring will retry automatically.")
            except Exception:
                pass

    def _alert_due(self, follow_id: str, category: str) -> bool:
        now = time.time()
        changed = self.db.db.execute(
            "INSERT INTO follow_alerts VALUES (?,?,?) ON CONFLICT(follow_id,category) "
            "DO UPDATE SET warned_at=excluded.warned_at "
            "WHERE follow_alerts.warned_at<? RETURNING warned_at",
            (follow_id, category, now, now - 86400),
        ).fetchone()
        return changed is not None

    async def _source_health(self, row, category: str, healthy: bool) -> None:
        if healthy:
            self.db.db.execute("DELETE FROM follow_health WHERE follow_id=? AND category=?",
                               (row["id"], category))
            return
        self.db.db.execute("INSERT INTO follow_health VALUES (?,?,1,0) "
                           "ON CONFLICT(follow_id,category) DO UPDATE SET failures=failures+1",
                           (row["id"], category))
        entry = self.db.db.execute("SELECT failures,warned FROM follow_health "
                                   "WHERE follow_id=? AND category=?",
                                   (row["id"], category)).fetchone()
        if category == "stories":
            # No story or a blocked anonymous story endpoint is not an
            # account change. Keep the status for explicit checks, no alert.
            return
        if entry["failures"] >= 3 and not entry["warned"] and self._alert_due(
                row["id"], category):
            self.db.db.execute("UPDATE follow_health SET warned=1 WHERE follow_id=? AND category=?",
                               (row["id"], category))
            try:
                await self.bot.send_message(row["chat_id"],
                    f"⚠️ Follow @{html.escape(row['handle'])}: public {html.escape(category)} "
                    "checks are unavailable. Other selected categories will keep working.")
            except Exception:
                pass

    async def check(self, row) -> dict[str, bool]:
        target = Target(row["platform"], row["handle"], row["url"])
        selected = set(json.loads(row["features"]))
        baselines = {r[0] for r in self.db.db.execute(
            "SELECT category FROM follow_baselines WHERE follow_id=?", (row["id"],))}
        batches: list[tuple[str, str, str]] = []
        health: dict[str, bool] = {}
        errors: dict[str, str] = {}
        content_categories = [key for key in ("posts", "reels", "tweets", "videos", "shorts", "stories", "live")
                              if key in selected]
        # Treat Instagram Posts and Reels as separate sources; all others use
        # their own platform-specific listing. A failed source does not erase
        # another category's baseline.
        fetch_categories = []
        for key in content_categories:
            query = "reels" if key == "reels" else (
                "posts" if key in {"posts", "tweets", "videos"} else key)
            if query not in fetch_categories:
                fetch_categories.append(query)
        fetched_ok: dict[str, bool] = {}
        # Source discovery runs concurrently within a small, fixed budget.
        # One blocked Stories endpoint no longer delays checking fresh Posts.
        gate = asyncio.Semaphore(3)

        async def fetch_content(kind: str):
            async with gate:
                try:
                    return kind, await public_items(target, kind), None
                except (ValueError, TimeoutError, OSError, json.JSONDecodeError,
                        httpx.HTTPError) as exc:
                    return kind, None, exc

        async def fetch_profile():
            async with gate:
                try:
                    return "profile", await public_profile(target), None
                except (ValueError, TimeoutError, OSError, httpx.HTTPError) as exc:
                    return "profile", None, exc

        operations = [fetch_content(kind) for kind in fetch_categories]
        if selected & {"bio", "name", "avatar"}:
            operations.append(fetch_profile())
        # asyncio.gather cancels all outstanding requests if this check is
        # cancelled by shutdown; all subprocess extractors have TERM handling.
        outcomes = await asyncio.gather(*operations)
        profile: dict[str, str] = {}
        for kind, payload, exc in outcomes:
            if exc is not None:
                if kind == "profile":
                    health["profile"] = False
                else:
                    fetched_ok[kind] = False
                errors[kind] = str(exc)[:160] if isinstance(exc, ValueError) else type(exc).__name__
                continue
            if kind == "profile":
                profile = payload
                health["profile"] = bool(profile)
                continue
            items = payload
            previously_seen = kind != "live" and any(
                self.db.db.execute(
                    "SELECT 1 FROM follow_seen WHERE follow_id=? AND event_key LIKE ? LIMIT 1",
                    (row["id"], f"{event_kind}:%")).fetchone()
                for event_kind in content_categories if (
                    kind == event_kind or kind == "posts" and
                    event_kind in {"posts", "reels", "tweets", "videos"}))
            if not items and previously_seen:
                fetched_ok[kind] = False
                errors[kind] = "previously populated timeline unexpectedly empty"
                continue
            batches.extend(e for e in items if e[0] in selected)
            fetched_ok[kind] = True
        for kind in content_categories:
            source_kind = "reels" if kind == "reels" else (
                "posts" if kind in {"posts", "tweets", "videos"} else kind)
            health[kind] = fetched_ok.get(source_kind, False)
        if health.get("shorts") and health.get("videos"):
            short_urls = {url for event_kind, url, _ in batches if event_kind == "shorts"}
            batches = [event for event in batches
                       if event[0] != "videos" or event[1] not in short_urls]
        for category, healthy in health.items():
            if healthy:
                self.db.db.execute("DELETE FROM follow_source_errors WHERE follow_id=? AND category=?",
                                   (row["id"], category))
            else:
                mapped_category = "posts" if category in {"tweets", "videos"} else category
                reason = errors.get(mapped_category, "provider unavailable")
                self.db.db.execute("INSERT INTO follow_source_errors VALUES (?,?,?,?) "
                                   "ON CONFLICT(follow_id,category) DO UPDATE SET "
                                   "reason=excluded.reason,checked_at=excluded.checked_at",
                                   (row["id"], category, reason, time.time()))
            await self._source_health(row, category, healthy)
        if not any(health.values()):
            raise ValueError("no public source accessible")
        old_profile = json.loads(row["profile_data"])
        started_row = self.db.db.execute(
            "SELECT started FROM follow_start WHERE follow_id=?", (row["id"],)
        ).fetchone()
        started = float(started_row[0]) if started_row else 0.0
        for event_kind, url, title in reversed(batches):
            if event_kind not in selected:
                continue
            key = f"{event_kind}:{url}"
            if self.db.db.execute("SELECT 1 FROM follow_seen WHERE follow_id=? AND event_key=?",
                                  (row["id"], key)).fetchone():
                continue
            new_since_follow = (event_kind not in baselines and started > 0
                                and (posted := _post_timestamp(url)) is not None
                                and started <= posted <= time.time() + 300)
            if event_kind in baselines or new_since_follow:
                await self.deliver(row, target, event_kind, url, title)
            self.db.db.execute("INSERT OR IGNORE INTO follow_seen VALUES (?,?)", (row["id"], key))
        for key in selected & {"bio", "name", "avatar"}:
            if key in baselines and key in profile and key in old_profile and profile[key] != old_profile[key]:
                if (key == "avatar" and target.platform == "tiktok" and
                        str(old_profile[key]).startswith(("http://", "https://"))):
                    # First check after upgrading the detector re-baselines an old
                    # signed URL without inventing an avatar edit.
                    continue
                value = "Avatar updated" if key == "avatar" else html.escape(profile[key][:500])
                token = secrets.token_urlsafe(8)
                get_token = secrets.token_urlsafe(7)
                buttons = [[InlineKeyboardButton(text="⬇️ Get it", callback_data=f"fd:{row['id']}:{get_token}"),
                            InlineKeyboardButton(text="🔗 View original", url=target.url)]]
                sent = await self.bot.send_message(row["chat_id"],
                    f"👀 <b>{target.platform.title()} · @{html.escape(target.handle)}</b>\n"
                    f"{LABELS[key]}\n\n{value}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
                self.db.db.execute("INSERT INTO follow_downloads "
                    "(token,follow_id,user_id,url,expires,kind,value) VALUES (?,?,?,?,?,?,?)",
                    (get_token, row["id"], row["user_id"], target.url,
                     time.time() + 7 * 86400, key, str(profile[key])[:500]))
        categories = [key for key in selected if key in health and health[key] and key != "profile"]
        if health.get("profile"):
            categories.extend(key for key in selected & {"bio", "name", "avatar"} if key in profile)
        self.db.db.executemany("INSERT OR IGNORE INTO follow_baselines VALUES (?,?)",
                               ((row["id"], key) for key in categories))
        old_profile.update(profile)
        self.db.db.execute("UPDATE follows SET baseline=1,profile_data=?,next_check=?,failures=0 "
                           "WHERE id=?", (json.dumps(old_profile), time.time() + INTERVAL, row["id"]))
        self.db.db.execute("DELETE FROM follow_seen WHERE follow_id=? AND rowid NOT IN "
                           "(SELECT rowid FROM follow_seen WHERE follow_id=? ORDER BY rowid DESC LIMIT 100)",
                           (row["id"], row["id"]))
        return health

    async def deliver(self, row, target: Target, kind: str, url: str, title: str,
                      event_key: str | None = None) -> None:
        if self.store.access_registry is not None:
            if not self._enabled(int(row["chat_id"])):
                return
            chat_type = "private" if row["chat_id"] == row["user_id"] else "group"
            if not self.store.can_access(row["user_id"], row["chat_id"], chat_type):
                return
        notice_token = secrets.token_urlsafe(8)
        get_token = secrets.token_urlsafe(7)
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬇️ Get it", callback_data=f"fd:{row['id']}:{get_token}"),
            InlineKeyboardButton(text="🔗 View original", url=url),
        ]])
        sent = await self.bot.send_message(row["chat_id"],
            f"👀 <b>{target.platform.title()} · @{html.escape(target.handle)}</b>\n"
            f"New {html.escape(kind)}{': ' + html.escape(title[:200]) if title else ''}",
            reply_markup=markup)
        self.db.db.execute("DELETE FROM follow_downloads WHERE expires<?", (time.time(),))
        self.db.db.execute("INSERT INTO follow_downloads "
            "(token,follow_id,user_id,url,expires,kind,value) VALUES (?,?,?,?,?,?,?)",
            (get_token, row["id"], row["user_id"], url, time.time() + 7 * 86400,
             kind, title[:2000]))
