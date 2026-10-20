#!/usr/bin/env python3
"""Read-only provider and media preflight. Never starts a Telegram poller.

Use the installed release's Python interpreter. Outputs only counts and error
classes, never provider URLs, cookies, Telegram sessions, or signed media URLs.
"""

import argparse
import asyncio
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mikachan.follow import FEATURES, command_target, public_items, public_profile
from mikachan.security import clean_url, source


def error_kind(exc: BaseException) -> str:
    """Map unstable provider exceptions to safe operational categories."""
    detail = str(exc).lower()
    if any(word in detail for word in ("configuration is incomplete", "session is missing",
                                       "staging chat must", "temporary directory is missing")):
        return "configuration_incomplete"
    if any(word in detail for word in ("401", "unauthorized", "login required")):
        return "authentication_required"
    if any(word in detail for word in ("403", "forbidden", "challenge", "captcha")):
        return "access_challenged"
    if "410" in detail:
        return "source_gone_or_blocked"
    if any(word in detail for word in ("429", "rate limit", "too many requests")):
        return "rate_limited"
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) or "timed out" in detail:
        return "timeout"
    if "dns" in detail or "connect" in detail:
        return "network_unavailable"
    return "provider_or_extractor_error"


def _env_keys(path: Path) -> dict[str, str]:
    """Inspect configuration readiness without loading or reporting secret values."""
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line.strip())
            if match:
                values[match[1]] = match[2].strip().strip("'\"")
    return values


def readiness(path: Path) -> dict[str, object]:
    values = _env_keys(path)

    def value(key: str) -> str:
        return values.get(key) or os.environ.get(key, "")

    stage = value("USERBOT_STAGING_CHAT_ID")
    private_stage = bool(re.fullmatch(r"-\d+", stage) and int(stage) < 0)
    credentials = bool(value("TELEGRAM_API_ID") and value("TELEGRAM_API_HASH"))
    session = bool(value("USERBOT_SESSION") or value("COINPJ_SESSION"))
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    return {
        "config_exists": path.exists(),
        "config_private": mode is not None and mode & 0o027 == 0,
        "api_credentials_present": credentials,
        "user_session_present": session,
        "private_staging_chat_configured": private_stage,
        "large_media_route_ready_on_paper": credentials and session and private_stage,
        "large_media_live_copy_verified": False,
        "ffmpeg_present": shutil.which("ffmpeg") is not None,
        "ffprobe_present": shutil.which("ffprobe") is not None,
        "yt_dlp_present": importlib.util.find_spec("yt_dlp") is not None,
        "gallery_dl_present": importlib.util.find_spec("gallery_dl") is not None,
    }


async def follow_probe(platform: str, handle: str, timeout: int) -> dict[str, object]:
    target = command_target(f"{platform} {handle}")
    if target is None:
        raise ValueError("Invalid supported platform or profile handle")
    gate = asyncio.Semaphore(3)
    categories = [item for item in FEATURES[target.platform]
                  if item not in {"bio", "name", "avatar"}]

    async def content(kind: str) -> tuple[str, dict[str, object]]:
        query = "posts" if kind in {"tweets", "videos"} else kind
        async with gate:
            begin = time.monotonic()
            try:
                items = await asyncio.wait_for(public_items(target, query), timeout)
                return kind, {"state": "items_found" if items else "empty_unverified",
                              "count": len(items), "seconds": round(time.monotonic() - begin, 2)}
            except (Exception, TimeoutError) as exc:
                return kind, {"state": "unavailable", "reason": error_kind(exc),
                              "seconds": round(time.monotonic() - begin, 2)}

    async def profile() -> tuple[str, dict[str, object]]:
        async with gate:
            begin = time.monotonic()
            try:
                data = await asyncio.wait_for(public_profile(target), timeout)
                return "profile", {"state": "fields_found" if data else "empty_unverified",
                                   "fields": sorted(k for k, v in data.items() if v),
                                   "seconds": round(time.monotonic() - begin, 2)}
            except (Exception, TimeoutError) as exc:
                return "profile", {"state": "unavailable", "reason": error_kind(exc),
                                   "seconds": round(time.monotonic() - begin, 2)}

    results = await asyncio.gather(*(content(kind) for kind in categories), profile())
    return {"platform": target.platform, "handle": target.handle,
            "read_only": True, "checks": dict(results),
            "note": "A found listing is not proof of complete or timely coverage."}


def follow_db_health(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"database_exists": False}
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        count = conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0]
        rows = conn.execute(
            "SELECT f.platform, e.category, COUNT(*) FROM follow_source_errors e "
            "JOIN follows f ON f.id=e.follow_id GROUP BY f.platform,e.category "
            "ORDER BY f.platform,e.category"
        ).fetchall()
        due = conn.execute("SELECT COUNT(*) FROM follows WHERE next_check<?",
                           (time.time(),)).fetchone()[0]
        return {"database_exists": True, "follows": count, "due_checks": due,
                "source_errors": [{"platform": p, "category": c, "count": n}
                                  for p, c, n in rows]}
    finally:
        conn.close()


def load_provider_cookie_paths(path: Path) -> None:
    """Load only provider cookie paths; never load bot tokens into the probe."""
    keys = ("MIKACHAN_INSTAGRAM_COOKIES_FILE", "MIKACHAN_X_FOLLOW_COOKIES_FILE",
            "MIKACHAN_TIKTOK_FOLLOW_COOKIES_FILE", "MIKACHAN_PORNHUB_COOKIES_FILE")
    values = _env_keys(path)
    for key in keys:
        if values.get(key) and key not in os.environ:
            os.environ[key] = values[key]


def media_probe(url: str, timeout: int, cookie_file: Path | None) -> dict[str, object]:
    url = clean_url(url)
    provider = source(url)
    if provider not in {"youtube", "pornhub"}:
        raise ValueError("Only YouTube and Pornhub metadata probes are supported")
    if cookie_file and (not cookie_file.is_file() or
                        stat.S_IMODE(cookie_file.stat().st_mode) & 0o077):
        raise ValueError("Cookie file must exist and be owner-private (chmod 600)")
    command = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
               "--skip-download", "--dump-single-json", "--no-warnings",
               "--socket-timeout", "12"]
    if cookie_file:
        command.extend(["--cookies", str(cookie_file)])
    command.append(url)
    start = time.monotonic()
    try:
        run = subprocess.run(command, capture_output=True, text=True,
                             timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"provider": provider, "state": "unavailable", "reason": error_kind(exc)}
    if run.returncode or len(run.stdout) > 8_000_000:
        return {"provider": provider, "state": "unavailable",
                "reason": error_kind(ValueError(run.stderr)),
                "seconds": round(time.monotonic() - start, 2)}
    try:
        data = json.loads(run.stdout)
    except json.JSONDecodeError:
        return {"provider": provider, "state": "invalid_extractor_output"}
    formats = data.get("formats") if isinstance(data, dict) else []
    if not isinstance(formats, list):
        formats = []
    # Some site extractors expose a verified height but omit codec metadata.
    video_formats = [f for f in formats if isinstance(f, dict)
                     and f.get("vcodec") != "none" and
                     isinstance(f.get("height"), int)]
    heights = sorted({f["height"] for f in video_formats})
    progressive = sorted({f["height"] for f in video_formats
                          if f.get("protocol") in {"https", "http"} and
                          f.get("ext") == "mp4"})
    return {"provider": provider,
            "state": "formats_available" if heights else "no_video_formats",
            "resolutions": heights, "progressive_mp4_resolutions": progressive,
            "choices_with_source_at_or_below": {
                str(height): max((h for h in heights if h <= height), default=None)
                for height in ((480, 720, 1080) if provider == "pornhub"
                               else (360, 480, 720, 1080))},
            "seconds": round(time.monotonic() - start, 2),
            "note": "Metadata only; no media bytes or Telegram delivery tested."}


async def transport_probe(config: Path, temp_root: Path) -> dict[str, object]:
    """Explicit live copy test; deletes both staged and destination messages."""
    from aiogram import Bot
    from mikachan.userbot import UserbotBridge

    values = _env_keys(config)
    needed = ("BOT_TOKEN", "OWNER_TELEGRAM_ID", "TELEGRAM_API_ID",
              "TELEGRAM_API_HASH", "USERBOT_STAGING_CHAT_ID")
    if any(not values.get(key) for key in needed):
        raise ValueError("Userbot transport configuration is incomplete")
    session = values.get("USERBOT_SESSION") or values.get("COINPJ_SESSION")
    if not session:
        raise ValueError("Userbot session is missing")
    if not re.fullmatch(r"-\d+", values["USERBOT_STAGING_CHAT_ID"]):
        raise ValueError("Staging chat must be a negative numeric ID")
    if not temp_root.is_dir() or temp_root.is_symlink():
        raise ValueError("Managed temporary directory is missing")
    bridge = UserbotBridge(int(values["TELEGRAM_API_ID"]),
                           values["TELEGRAM_API_HASH"], session,
                           int(values["USERBOT_STAGING_CHAT_ID"]))
    bot = Bot(values["BOT_TOKEN"])
    copied = None
    removed = False
    try:
        with tempfile.TemporaryDirectory(prefix="transport-probe-", dir=temp_root) as temp:
            path = Path(temp) / "mikachan-probe.txt"
            path.write_text("Mikachan transport probe\n", encoding="utf-8")
            try:
                copied = await asyncio.wait_for(
                    bridge.upload_and_copy(bot, path, int(values["OWNER_TELEGRAM_ID"]),
                                           "Mikachan transport probe", None), timeout=75)
            finally:
                if copied is not None:
                    try:
                        removed = await bot.delete_message(
                            int(values["OWNER_TELEGRAM_ID"]), copied.message_id)
                    except Exception:
                        removed = False
        return {"state": "copy_verified", "destination_probe_deleted": removed,
                "staging_cleanup_attempted": True}
    finally:
        await bot.session.close()
        await bridge.client.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ready = commands.add_parser("readiness", help="Inspect local userbot prerequisites")
    ready.add_argument("--env", type=Path, default=Path("/opt/Mikachan/config/bot.env"))
    follow = commands.add_parser("follow", help="Probe each public Follow category")
    follow.add_argument("platform", choices=sorted(FEATURES))
    follow.add_argument("handle")
    follow.add_argument("--timeout", type=int, default=45)
    follow.add_argument("--env", type=Path, default=Path("/opt/Mikachan/config/bot.env"))
    health = commands.add_parser("follow-db", help="Read-only stored Follow health")
    health.add_argument("--db", type=Path,
                        default=Path("/opt/Mikachan/state/mikachan.sqlite"))
    media = commands.add_parser("media", help="Probe formats without downloading media")
    media.add_argument("url")
    media.add_argument("--timeout", type=int, default=70)
    media.add_argument("--cookies", type=Path)
    media.add_argument("--env", type=Path, default=Path("/opt/Mikachan/config/bot.env"))
    transport = commands.add_parser("transport", help="Explicit live Telegram staging copy test")
    transport.add_argument("--live", action="store_true", required=True)
    transport.add_argument("--env", type=Path, default=Path("/opt/Mikachan/config/bot.env"))
    transport.add_argument("--tmp", type=Path, default=Path("/opt/Mikachan/tmp/manager"))
    args = parser.parse_args()
    try:
        if args.command == "readiness":
            result = readiness(args.env)
        elif args.command == "follow":
            if not 5 <= args.timeout <= 120:
                raise ValueError("Timeout must be 5–120 seconds")
            load_provider_cookie_paths(args.env)
            result = asyncio.run(follow_probe(args.platform, args.handle, args.timeout))
        elif args.command == "follow-db":
            result = follow_db_health(args.db)
        elif args.command == "transport":
            result = asyncio.run(transport_probe(args.env, args.tmp))
        else:
            if not 5 <= args.timeout <= 180:
                raise ValueError("Timeout must be 5–180 seconds")
            load_provider_cookie_paths(args.env)
            configured = os.environ.get("MIKACHAN_PORNHUB_COOKIES_FILE", "")
            cookie_file = args.cookies or (Path(configured) if configured and
                                           source(clean_url(args.url)) == "pornhub" else None)
            result = media_probe(args.url, args.timeout, cookie_file)
    except Exception as exc:
        print(json.dumps({"state": "error", "reason": error_kind(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
