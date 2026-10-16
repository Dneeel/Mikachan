"""Listening links and verified public lyric lookup for music search results."""

import re
from urllib.parse import quote

import httpx

from .matching import match_score


def listening_links(title: str, creator: str, source: str, url: str) -> list[tuple[str, str]]:
    query = quote(f"{creator} {title}".strip(), safe="")
    links = [
        ("Spotify" if source == "spotify" else "Spotify 🔎",
         url if source == "spotify" else f"https://open.spotify.com/search/{query}"),
        ("YouTube Music" if source == "ytmusic" else "YouTube Music 🔎",
         url if source == "ytmusic" else f"https://music.youtube.com/search?q={query}"),
        ("SoundCloud" if source == "soundcloud" else "SoundCloud 🔎",
         url if source == "soundcloud" else f"https://soundcloud.com/search/sounds?q={query}"),
    ]
    return links


def _plain_lyrics(value: str) -> str:
    lines = [re.sub(r"^\[\d{1,2}:\d{2}(?:\.\d+)?\]\s*", "", line).strip()
             for line in value.splitlines()]
    return "\n".join(lines).strip()


async def verified_lyrics(title: str, creator: str, duration: int | None = None) -> str | None:
    """Prefer a complete, correctly attributed lyric over a short first search hit."""
    if not title.strip() or not creator.strip():
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(
                "https://lrclib.net/api/search",
                params={"track_name": title, "artist_name": creator},
                headers={"User-Agent": "MIKA-NEXUS/1.0 (lyrics lookup)"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            payload = []
        candidates: list[tuple[int, str]] = []
        if isinstance(payload, list):
            for item in payload[:50]:
                if not isinstance(item, dict):
                    continue
                matched_title = str(item.get("trackName") or "")
                matched_artist = str(item.get("artistName") or "")
                if match_score(title, creator, matched_title, matched_artist) < 0.85:
                    continue
                item_duration = item.get("duration")
                if duration and isinstance(item_duration, (int, float)) and abs(duration - item_duration) > 15:
                    continue
                lyric = item.get("plainLyrics") or item.get("syncedLyrics")
                if isinstance(lyric, str):
                    plain = _plain_lyrics(lyric)
                    lines = sum(bool(line.strip()) for line in plain.splitlines())
                    if lines:
                        candidates.append((lines, plain))
        if candidates:
            best_lines, best = max(candidates, key=lambda pair: (pair[0], len(pair[1])))
            if best_lines >= 8:
                return best
        try:
            response = await client.get(
                f"https://api.lyrics.ovh/v1/{quote(creator, safe='')}/{quote(title, safe='')}"
            )
            response.raise_for_status()
            lyric = response.json().get("lyrics")
            if isinstance(lyric, str):
                plain = _plain_lyrics(lyric)
                if sum(bool(line.strip()) for line in plain.splitlines()) >= 8:
                    return plain
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
    return None
