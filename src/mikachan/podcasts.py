"""Bounded public RSS episode browsing with redirect and DNS checks."""

import re
from difflib import SequenceMatcher
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
import unicodedata

import feedparser  # type: ignore[import-untyped]
import httpx

from .security import clean_url, validate_dns
from .catalog import _Meta


def is_feed_url(url: str) -> bool:
    path = urlsplit(url).path.lower().rstrip("/")
    return path.endswith((".xml", ".rss")) or path.endswith(("/feed", "/rss", "/feed.xml"))


async def episodes(url: str) -> tuple[list[dict[str, str]], str]:
    current = clean_url(url)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        for _ in range(5):
            validate_dns(current)
            async with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("RSS redirect has no destination")
                    current = clean_url(urljoin(current, location))
                    continue
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes(65536):
                    content.extend(chunk)
                    if len(content) > 2_000_000:
                        raise ValueError("RSS feed is too large")
                break
        else:
            raise ValueError("too many RSS redirects")
    parsed = feedparser.parse(bytes(content))
    if not parsed.entries:
        raise ValueError("No public podcast episodes found")
    items: list[dict[str, str]] = []
    feed_title = str(parsed.feed.get("title") or "Podcast")[:150]
    feed_image = str((parsed.feed.get("image") or {}).get("href") or "")
    for entry in parsed.entries[:300]:
        enclosures = entry.get("enclosures", [])
        audio = next(
            (
                x.get("href")
                for x in enclosures
                if x.get("href")
                and (
                    str(x.get("type", "")).startswith("audio/")
                    or re.search(r"\.(mp3|m4a|ogg|opus)(?:\?|$)", str(x.get("href")), re.I)
                )
            ),
            None,
        )
        if not audio:
            continue
        try:
            target = clean_url(str(audio))
        except ValueError:
            continue
        title = str(entry.get("title") or "Episode")[:180]
        try:
            source_url = clean_url(str(entry.get("link") or current))
        except ValueError:
            source_url = current
        image = str((entry.get("image") or {}).get("href") or feed_image)
        items.append({
            "title": title, "artist": feed_title, "url": target,
            "source_url": source_url, "cover": image,
            "description": str(entry.get("summary") or "")[:12000],
            "kind": "episode", "date": str(entry.get("published") or "")[:24],
        })
    if not items:
        raise ValueError("No downloadable public podcast episodes found")
    return items, feed_title


async def apple_podcast_feed(url: str) -> str:
    match = re.search(r"/id(\d+)", urlsplit(url).path)
    if not match:
        raise ValueError("Apple podcast ID missing")
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.get("https://itunes.apple.com/lookup", params={"id": match[1]})
        response.raise_for_status()
        results = response.json().get("results", [])
    feed_url = next((x.get("feedUrl") for x in results if x.get("feedUrl")), None)
    if not feed_url:
        raise ValueError("Apple podcast feed unavailable")
    return clean_url(feed_url)


def _similar(a: str, b: str) -> float:
    def normalize(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold().replace("\u200c", " ")
        return " ".join(re.findall(r"[^\W_]+", value, re.UNICODE))
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


async def resolve_episode_link(url: str) -> dict[str, str | int] | None:
    """Resolve a specific Apple, Spotify, or Castbox episode to public audio."""
    clean = clean_url(url)
    host = urlsplit(clean).hostname or ""
    castbox = host == "castbox.fm" or host.endswith(".castbox.fm")
    if not (
        host == "podcasts.apple.com" and "i=" in urlsplit(clean).query
        or host == "open.spotify.com" and urlsplit(clean).path.startswith("/episode/")
        or castbox and bool(re.search(r"/(?:vi|ep)/\d+|/episode/.+-id\d+", urlsplit(clean).path))
    ):
        return None
    current = clean
    if castbox:
        short = re.search(r"/(?:vi|ep)/(\d+)", urlsplit(clean).path)
        if short:
            current = f"https://castbox.fm/ep/{short[1]}"
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        for _ in range(4):
            validate_dns(current)
            page = await client.get(current, headers={"User-Agent": "Mozilla/5.0"})
            if page.status_code not in {301, 302, 303, 307, 308}:
                page.raise_for_status()
                break
            location = page.headers.get("location")
            if not location:
                raise ValueError("podcast episode redirect is incomplete")
            current = clean_url(urljoin(current, location))
        else:
            raise ValueError("too many podcast episode redirects")
        parser = _Meta()
        parser.feed(page.text[:500_000])
        title = parser.data.get("og:title", "").strip()
        description = parser.data.get("og:description", "")
        if castbox:
            slug = unquote(urlsplit(current).path).split("/episode/")[-1]
            slug = re.sub(r"-id\d+(?:-id\d+)?$", "", slug)
            title = slug.replace("-", " ").strip()
            description = ""
        if not title:
            raise ValueError("episode title is unavailable")
        show = description.split(" · ")[0].strip()
        if host == "podcasts.apple.com" and show.lower() == "podcast episode":
            parts = description.split(" · ")
            show = parts[1].strip() if len(parts) > 1 else ""
        lookup = await client.get(
            "https://itunes.apple.com/search",
            params={"term": title, "entity": "podcastEpisode", "limit": 100},
        )
        lookup.raise_for_status()
    identifier = parse_qs(urlsplit(current).query).get("i", [""])[0]
    candidates = lookup.json().get("results", [])
    matches = [
        episode for episode in candidates
        if episode.get("episodeUrl")
        and (_similar(title, str(episode.get("trackName") or "")) >= (0.97 if castbox else 0.90))
        and (not show or _similar(show, str(episode.get("collectionName") or "")) >= 0.70)
    ]
    if identifier:
        matches = [episode for episode in matches if str(episode.get("trackId")) == identifier]
    if castbox and len({str(x.get("episodeUrl")) for x in matches}) != 1:
        raise ValueError("Castbox episode identity could not be verified in public sources")
    if not matches:
        raise ValueError("No matching public version of this episode was found")
    match = None
    for candidate in matches[:5]:
        audio_url = clean_url(str(candidate["episodeUrl"]))
        validate_dns(audio_url)
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as probe:
                check = await probe.head(audio_url)
                if check.status_code in {404, 410}:
                    # Some CDNs disable HEAD even when a ranged GET works.
                    async with probe.stream("GET", audio_url,
                                            headers={"Range": "bytes=0-0"}) as sample:
                        if sample.status_code in {404, 410}:
                            continue
        except httpx.HTTPError:
            # Some hosts reject probes while allowing the actual media GET.
            match = candidate
            break
        match = candidate
        break
    if match is None:
        raise ValueError("The episode's public audio link is no longer available")
    return {
        "url": clean_url(str(match["episodeUrl"])),
        "title": str(match.get("trackName") or title),
        "artist": str(match.get("collectionName") or show),
        "duration": int(match.get("trackTimeMillis") or 0) // 1000,
        "cover": str(match.get("artworkUrl600") or parser.data.get("og:image") or ""),
        "source_url": clean, "kind": "episode",
        "description": description,
    }
