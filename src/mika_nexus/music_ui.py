"""Listening links and verified public lyric lookup for music search results."""

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


async def verified_lyrics(title: str, creator: str) -> str | None:
    """Return attributed lyrics only when the catalog title and artist agree."""
    if not title.strip() or not creator.strip():
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            "https://lrclib.net/api/search",
            params={"track_name": title, "artist_name": creator},
            headers={"User-Agent": "MIKA-NEXUS/1.0 (lyrics lookup)"},
        )
        response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        return None
    for item in payload[:10]:
        if not isinstance(item, dict):
            continue
        matched_title = str(item.get("trackName") or "")
        matched_artist = str(item.get("artistName") or "")
        if match_score(title, creator, matched_title, matched_artist) < 0.85:
            continue
        text = item.get("plainLyrics") or item.get("syncedLyrics")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None
