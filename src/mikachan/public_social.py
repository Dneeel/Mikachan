"""Bounded public social discovery through the current FxEmbed endpoints.

These endpoints are best effort public sources. Every result is checked against
the requested creator or post before it can create a follow event or download.
"""

import asyncio
import re
import time
from urllib.parse import urlsplit

import httpx

from .security import clean_url, validate_dns

_BASE = {"twitter": "https://api.fxtwitter.com", "tiktok": "https://api.atmosphere.tools"}
_MEDIA_HOSTS = {"pbs.twimg.com", "video.twimg.com"}


async def _get(platform: str, path: str) -> dict:
    url = clean_url(_BASE[platform] + path)
    await asyncio.to_thread(validate_dns, url)
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        for attempt in range(2):
            response = await client.get(url, headers={"Accept": "application/json"})
            if response.status_code in {404, 429, 500, 502, 503, 504} and attempt == 0:
                await asyncio.sleep(0.5)
                continue
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise ValueError("public social response is too large")
            data = response.json()
            break
    if not isinstance(data, dict) or data.get("code") != 200:
        raise ValueError("public social response is unavailable")
    return data


async def public_statuses(platform: str, handle: str) -> list[tuple[str, str, str]]:
    if platform not in _BASE or not re.fullmatch(r"[\w.]{1,64}", handle, re.ASCII):
        raise ValueError("invalid public profile")
    path = (f"/2/tiktok/profile/{handle}/statuses?count=10" if platform == "tiktok"
            else f"/2/profile/{handle}/statuses?count=15")
    data = await _get(platform, path)
    results = data.get("results")
    if not isinstance(results, list):
        raise ValueError("invalid public timeline")
    posts: list[tuple[int, str, str, str]] = []
    for item in results[:40]:
        if not isinstance(item, dict) or not isinstance(item.get("author"), dict):
            continue
        if str(item["author"].get("screen_name") or "").casefold() != handle.casefold():
            continue
        ident = str(item.get("id") or "")
        if not re.fullmatch(r"\d{10,22}", ident):
            continue
        kind = "tweets" if platform == "twitter" else "posts"
        url = (f"https://x.com/{handle}/status/{ident}" if platform == "twitter"
               else f"https://www.tiktok.com/@{handle}/video/{ident}")
        stamp = item.get("created_timestamp")
        posts.append((int(stamp) if isinstance(stamp, (int, float)) else 0,
                      kind, url, str(item.get("text") or "")[:2000]))
    posts.sort(key=lambda item: (item[0], item[2]), reverse=True)
    return [(kind, url, title) for _, kind, url, title in posts[:15]]


async def public_tiktok_profile(handle: str, *, display_avatar: bool = False) -> dict[str, str]:
    """Use one identity-checked public profile response for stable change signals."""
    if not re.fullmatch(r"[\w.]{1,64}", handle, re.ASCII):
        raise ValueError("invalid TikTok handle")
    data = await _get("tiktok", f"/2/tiktok/profile/{handle}")
    user = data.get("user")
    if (not isinstance(user, dict) or
            str(user.get("screen_name") or "").casefold() != handle.casefold() or
            user.get("protected") is True):
        raise ValueError("TikTok public profile identity mismatch")
    result = {"name": str(user.get("name") or "")[:500],
              "bio": str(user.get("description") or "")[:500]}
    avatar = str(user.get("avatar_url") or "")
    if avatar.startswith("https://"):
        if display_avatar:
            await asyncio.to_thread(validate_dns, avatar)
            result["avatar"] = avatar
        else:
            # CDN transforms and signed query parameters rotate independently
            # of profile changes. The embedded asset hash is a stable signal.
            match = re.search(r"/([a-f0-9]{32,})~", urlsplit(avatar).path)
            if match:
                result["avatar"] = match.group(1)
    return result


async def public_x_profile(handle: str, *, display_avatar: bool = False) -> dict[str, str]:
    """Use VxTwitter only when its cached profile is fresh and identity matches."""
    if not re.fullmatch(r"[\w.]{1,64}", handle, re.ASCII):
        raise ValueError("invalid X handle")
    endpoint = clean_url(f"https://api.vxtwitter.com/{handle}")
    await asyncio.to_thread(validate_dns, endpoint)
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.get(endpoint, headers={"Accept": "application/json"})
        response.raise_for_status()
        if len(response.content) > 300_000:
            raise ValueError("X profile response is too large")
        data = response.json()
    if (not isinstance(data, dict) or
            str(data.get("screen_name") or "").casefold() != handle.casefold() or
            data.get("protected") is True):
        raise ValueError("X public profile identity mismatch")
    fetched = data.get("fetched_on")
    if not isinstance(fetched, (int, float)) or not 0 <= time.time() - fetched <= 7200:
        raise ValueError("X public profile snapshot is stale")
    result = {"name": str(data.get("name") or "")[:500],
              "bio": str(data.get("description") or "")[:500]}
    avatar = str(data.get("profile_image_url") or "")
    parts = urlsplit(avatar)
    if parts.scheme == "https" and parts.hostname == "pbs.twimg.com":
        if display_avatar:
            result["avatar"] = avatar
        else:
            result["avatar"] = re.sub(r"_(?:normal|bigger|mini)(?=\.[A-Za-z]{3,4}$)",
                                       "", parts.path)
    return result


async def public_x_post(url: str) -> tuple[str, list[str]] | None:
    """Return verified text and media for exactly the requested public X status."""
    clean = clean_url(url)
    parts = [part for part in urlsplit(clean).path.split("/") if part]
    if (len(parts) < 3 or parts[1] != "status"
            or not re.fullmatch(r"[\w.]{1,64}", parts[0], re.ASCII)
            or not re.fullmatch(r"\d{10,22}", parts[2])):
        return None
    try:
        data = await _get("twitter", f"/2/status/{parts[2]}")
    except (ValueError, httpx.HTTPError):
        return await _vxtwitter_post(parts[0], parts[2])
    status = data.get("status")
    if not isinstance(status, dict) or str(status.get("id")) != parts[2]:
        raise ValueError("public X status identity mismatch")
    author = status.get("author")
    if (not isinstance(author, dict)
            or str(author.get("screen_name") or "").casefold() != parts[0].casefold()):
        raise ValueError("public X author identity mismatch")
    media = status.get("media")
    items = media.get("all") if isinstance(media, dict) else None
    if not isinstance(items, list):
        items = []
    urls = []
    for item in items[:10]:
        if not isinstance(item, dict) or item.get("type") not in {"photo", "video", "gif"}:
            continue
        candidate = item.get("url")
        if not isinstance(candidate, str):
            continue
        candidate = clean_url(candidate)
        if urlsplit(candidate).hostname in _MEDIA_HOSTS and candidate.startswith("https://"):
            urls.append(candidate)
    text = status.get("text")
    if not isinstance(text, str):
        text = ""
    return (text, urls) if text or urls else None


async def public_x_media(url: str) -> list[str] | None:
    """Return only verified media for the requested X status."""
    post = await public_x_post(url)
    return (post[1] or None) if post else None


async def _vxtwitter_post(handle: str, status_id: str) -> tuple[str, list[str]] | None:
    """Independent public status lookup when FxTwitter challenges this server."""
    endpoint = f"https://api.vxtwitter.com/{handle}/status/{status_id}"
    await asyncio.to_thread(validate_dns, endpoint)
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        response = await client.get(endpoint, headers={"Accept": "application/json"})
        response.raise_for_status()
        if len(response.content) > 300_000:
            raise ValueError("public X status response is too large")
        data = response.json()
    if not isinstance(data, dict) or str(data.get("tweetID")) != status_id:
        raise ValueError("public X status identity mismatch")
    if str(data.get("user_screen_name") or "").casefold() != handle.casefold():
        raise ValueError("public X author identity mismatch")
    media = data.get("media_extended")
    if not isinstance(media, list):
        media = []
    urls: list[str] = []
    for item in media[:10]:
        if not isinstance(item, dict) or item.get("type") not in {"video", "image", "gif"}:
            continue
        raw = item.get("url")
        if not isinstance(raw, str):
            continue
        candidate = clean_url(raw)
        if candidate.startswith("https://") and urlsplit(candidate).hostname in _MEDIA_HOSTS:
            urls.append(candidate)
    text = data.get("text")
    if not isinstance(text, str):
        text = ""
    return (text, urls) if text or urls else None


async def public_tiktok_sound(sound_id: str) -> tuple[str, str, int]:
    """Find the sound's own public audio, checking the ID through two sources."""
    if not re.fullmatch(r"\d{10,22}", sound_id):
        raise ValueError("invalid TikTok sound ID")
    listing = await _get("tiktok", f"/2/tiktok/music/{sound_id}?count=3")
    collection = listing.get("collection")
    if not isinstance(collection, dict) or str(collection.get("id")) != sound_id:
        raise ValueError("TikTok sound identity could not be verified")
    results = listing.get("results")
    if not isinstance(results, list):
        raise ValueError("No public posts use this TikTok sound")
    endpoint = "https://www.tikwm.com/api/"
    await asyncio.to_thread(validate_dns, endpoint)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        for item in results[:3]:
            if not isinstance(item, dict):
                continue
            video_id = str(item.get("id") or "")
            video_url = item.get("url")
            if not re.fullmatch(r"\d{10,22}", video_id) or not isinstance(video_url, str):
                continue
            if not re.search(rf"/video/{video_id}(?:[/?]|$)", video_url):
                continue
            response = await client.get(endpoint, params={"url": clean_url(video_url)})
            response.raise_for_status()
            if len(response.content) > 250_000:
                continue
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("code") != 0:
                continue
            video = payload.get("data")
            if not isinstance(video, dict) or str(video.get("id")) != video_id:
                continue
            music = video.get("music_info")
            if not isinstance(music, dict) or str(music.get("id")) != sound_id:
                continue
            play = music.get("play")
            duration = music.get("duration")
            if not isinstance(play, str) or not play.startswith("https://"):
                continue
            if not isinstance(duration, (int, str)) or not str(duration).isdigit():
                continue
            seconds = int(duration)
            if not 1 <= seconds <= 7200:
                continue
            audio = clean_url(play)
            await asyncio.to_thread(validate_dns, audio)
            return audio, str(music.get("title") or "TikTok sound")[:180], seconds
    raise ValueError("This TikTok sound has no verified public audio file")
