"""Bounded public RSS episode browsing with redirect and DNS checks."""

import re
from urllib.parse import urljoin, urlsplit

import feedparser  # type: ignore[import-untyped]
import httpx

from .security import clean_url, validate_dns


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
    for entry in parsed.entries[:60]:
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
        title = str(entry.get("title") or "Episode")
        date = str(entry.get("published") or "")[:24]
        items.append({"title": f"{title} · {date}"[:100], "url": target})
        if len(items) >= 30:
            break
    if not items:
        raise ValueError("No downloadable public podcast episodes found")
    return items, str(parsed.feed.get("title") or "Podcast")[:150]


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
