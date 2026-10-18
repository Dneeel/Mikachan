"""Listening links and verified public lyric lookup for music search results."""

import re
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from .catalog import CatalogTrack, recording_search_alias
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
        variants = [(title, creator)]
        async def lrclib(wanted_title: str, wanted_artist: str) -> str | None:
            try:
                response = await client.get(
                    "https://lrclib.net/api/search",
                    params={"track_name": wanted_title, "artist_name": wanted_artist},
                    headers={"User-Agent": "Mikachan/1.0 (lyrics lookup)"},
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                return None
            if not isinstance(payload, list):
                return None
            candidates: list[tuple[int, str]] = []
            for item in payload[:50]:
                if not isinstance(item, dict):
                    continue
                matched_title = str(item.get("trackName") or "")
                matched_artist = str(item.get("artistName") or "")
                if match_score(wanted_title, wanted_artist, matched_title, matched_artist) < 0.83:
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
            return None

        first = await lrclib(title, creator)
        if first:
            return first
        alias = await recording_search_alias(CatalogTrack(title, creator, "", duration))
        if alias:
            variants.append(alias)
            found = await lrclib(*alias)
            if found:
                return found
        for wanted_title, wanted_artist in variants:
            try:
                response = await client.get(
                    f"https://api.lyrics.ovh/v1/{quote(wanted_artist, safe='')}/{quote(wanted_title, safe='')}"
                )
                response.raise_for_status()
                lyric = response.json().get("lyrics")
                if isinstance(lyric, str):
                    plain = _plain_lyrics(lyric)
                    if sum(bool(line.strip()) for line in plain.splitlines()) >= 8:
                        return plain
            except (httpx.HTTPError, ValueError, AttributeError):
                pass
        for wanted_title, wanted_artist in variants:
            found = await _genius_lyrics(client, wanted_title, wanted_artist)
            if found:
                return found
    return None


async def _genius_lyrics(client: httpx.AsyncClient, title: str, artist: str) -> str | None:
    """Read a full public lyric page only after validating its search metadata."""
    try:
        response = await client.get(
            "https://genius.com/api/search/multi",
            params={"q": f"{artist} {title}", "per_page": 5},
        )
        response.raise_for_status()
        sections = response.json().get("response", {}).get("sections", [])
        candidates = [hit.get("result", {}) for section in sections
                      for hit in section.get("hits", [])]
        for item in candidates:
            found_title = str(item.get("title") or "")
            found_artist = str((item.get("primary_artist") or {}).get("name") or "")
            url = str(item.get("url") or "")
            if (not found_title or not found_artist or not url.startswith("https://genius.com/")
                    or match_score(title, artist, found_title, found_artist) < 0.82):
                continue
            page = await client.get(url)
            page.raise_for_status()
            soup = BeautifulSoup(page.text, "html.parser")
            blocks = soup.select("[data-lyrics-container]")
            parts = []
            for block in blocks:
                for br in block.find_all("br"):
                    br.replace_with("\n")
                parts.append(block.get_text().strip())
            lyric = _plain_lyrics("\n".join(part for part in parts if part))
            if sum(bool(line.strip()) for line in lyric.splitlines()) >= 8:
                return lyric
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        pass
    return None
