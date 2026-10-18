"""Resolve public catalog links to a track identity; no protected audio is fetched."""

import asyncio
import base64
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx

from .security import clean_url, source, validate_dns
from .matching import tokens, version
from .config import ProviderSettings


@dataclass(frozen=True)
class CatalogTrack:
    title: str
    artist: str
    url: str
    duration: int | None = None
    isrc: str | None = None
    cover: str = ""


async def recording_search_alias(track: CatalogTrack) -> tuple[str, str] | None:
    """Find an alternate published title/artist for non-Latin catalog metadata."""
    if track.artist.isascii() and track.title.isascii():
        return None
    core = re.split(r"[（(]", track.title, maxsplit=1)[0].strip(" -")
    wanted = tokens(core)
    if not wanted:
        return None
    async with httpx.AsyncClient(timeout=8) as client:
        for term in (f"{core} {track.artist}", core, track.artist):
            try:
                response = await client.get(
                    "https://itunes.apple.com/search",
                    params={"term": term, "entity": "song", "limit": 30},
                )
                response.raise_for_status()
                results = response.json().get("results", [])
            except (httpx.HTTPError, ValueError):
                continue
            possible_aliases: list[tuple[str, str, float]] = []
            for item in results:
                title = str(item.get("trackName") or "")
                artist = str(item.get("artistName") or "")
                if not title or not artist or (title == track.title and artist == track.artist):
                    continue
                if version(title) != version(track.title):
                    continue
                item_duration = item.get("trackTimeMillis")
                if not track.duration or not item_duration:
                    continue
                delta = abs(track.duration - int(item_duration) / 1000)
                title_overlap = len(wanted & tokens(title)) / len(wanted)
                original_artist = tokens(track.artist)
                artist_overlap = (len(original_artist & tokens(artist)) / len(original_artist)
                                  if original_artist else 0.0)
                if title_overlap >= 0.7 and delta <= 6:
                    return title, artist
                if term != track.artist and artist_overlap >= 0.6 and delta <= 3:
                    possible_aliases.append((title, artist, delta))
            # A translated title has no shared tokens with its original script.
            # Artist and duration alone only identify it if this query yielded
            # one distinct title; otherwise an automatic alias could name a
            # different recording by the same artist.
            distinct = {(title.casefold(), artist.casefold()) for title, artist, _ in possible_aliases}
            if len(distinct) == 1:
                title, artist, _ = min(possible_aliases, key=lambda item: item[2])
                return title, artist
    return None


def _spotify_art(entity: dict) -> str:
    pictures = (entity.get("visualIdentity") or {}).get("image") or []
    return str(max(pictures, key=lambda x: x.get("maxWidth", 0)).get("url") or "") if pictures else ""


async def _spotify_headers(client: httpx.AsyncClient) -> dict[str, str] | None:
    providers = ProviderSettings()
    cid = providers.spotify_client_id
    secret = providers.spotify_client_secret
    if not cid or not secret:
        return None
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    token_response = await client.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": "Basic " + auth},
    )
    token_response.raise_for_status()
    return {"Authorization": "Bearer " + str(token_response.json()["access_token"])}


async def _spotify_show_collection(
    client: httpx.AsyncClient, identifier: str,
) -> tuple[list[dict[str, str]], str]:
    """Page an accessible Spotify show without substituting preview audio."""
    headers = await _spotify_headers(client)
    if headers is None:
        raise ValueError("Spotify show browsing requires configured catalog credentials")
    info = await client.get(
        f"https://api.spotify.com/v1/shows/{identifier}", headers=headers,
    )
    info.raise_for_status()
    show = info.json()
    title = str(show.get("name") or "Podcast")
    cover = str((show.get("images") or [{}])[0].get("url") or "")
    first = await client.get(
        f"https://api.spotify.com/v1/shows/{identifier}/episodes",
        params={"limit": 50, "offset": 0}, headers=headers,
    )
    first.raise_for_status()
    initial = first.json()
    all_count = int(initial.get("total") or len(initial.get("items") or []))
    total = min(all_count, 300)

    async def page(offset: int) -> dict:
        response = await client.get(
            f"https://api.spotify.com/v1/shows/{identifier}/episodes",
            params={"limit": 50, "offset": offset}, headers=headers,
        )
        response.raise_for_status()
        return response.json()

    pages = [initial]
    if total > 50:
        pages.extend(await asyncio.gather(*(page(i) for i in range(50, total, 50))))
    items: list[dict[str, str]] = []
    for batch in pages:
        for episode in batch.get("items") or []:
            if not isinstance(episode, dict):
                continue
            link = (episode.get("external_urls") or {}).get("spotify")
            if not link or not episode.get("id"):
                continue
            items.append({
                "title": str(episode.get("name") or "Episode")[:180],
                "artist": title[:100], "url": str(link), "source_url": str(link),
                "cover": str((episode.get("images") or [{}])[0].get("url") or cover),
                "duration": str(int(episode.get("duration_ms") or 0) // 1000),
                "kind": "episode", "description": str(episode.get("description") or "")[:12000],
                "date": str(episode.get("release_date") or ""),
            })
    if not items:
        raise ValueError("No public Spotify show episodes were returned")
    return items[:300], title + (f" · First 300 of {all_count}" if all_count > 300 else "")


async def _spotify_music_collection(
    client: httpx.AsyncClient, kind: str, identifier: str,
) -> tuple[list[dict[str, str]], str] | None:
    """Use official, paged metadata when optional catalog credentials exist."""
    headers = await _spotify_headers(client)
    if headers is None:
        return None
    response = await client.get(
        f"https://api.spotify.com/v1/{'albums' if kind == 'album' else 'playlists'}/{identifier}",
        headers=headers,
    )
    response.raise_for_status()
    info = response.json()
    title = str(info.get("name") or "Collection")
    paging = info.get("tracks") or {}
    first_items = paging.get("items") or []
    all_count = int(paging.get("total") or len(first_items))
    total = min(all_count, 300)
    page_size = 50 if kind == "album" else 100
    path = f"https://api.spotify.com/v1/{'albums' if kind == 'album' else 'playlists'}/{identifier}/tracks"

    async def page(offset: int) -> list[dict]:
        result = await client.get(
            path, params={"limit": page_size, "offset": offset}, headers=headers,
        )
        result.raise_for_status()
        return result.json().get("items") or []

    if not first_items and total:
        first_items = await page(0)
    entries = list(first_items)
    if total > len(first_items):
        pages = await asyncio.gather(
            *(page(offset) for offset in range(len(first_items), total, page_size))
        )
        for batch in pages:
            entries.extend(batch)
    owner = (info.get("owner") or {}).get("external_urls") or {}
    owner_url = str(owner.get("spotify") or "")
    collection_cover = str((info.get("images") or [{}])[0].get("url") or "")
    items: list[dict[str, str]] = []
    for entry in entries[:300]:
        track = entry.get("track") if kind == "playlist" else entry
        if not isinstance(track, dict) or track.get("type", "track") != "track":
            continue
        link = (track.get("external_urls") or {}).get("spotify")
        if not link or not track.get("id"):
            continue
        artists = ", ".join(
            str(artist.get("name") or "") for artist in track.get("artists") or []
            if isinstance(artist, dict) and artist.get("name")
        )
        album = track.get("album") or {}
        cover = str((album.get("images") or [{}])[0].get("url") or collection_cover)
        items.append({
            "title": str(track.get("name") or "Untitled")[:180],
            "artist": artists[:100], "url": str(link), "source_url": str(link),
            "duration": str(int(track.get("duration_ms") or 0) // 1000),
            "cover": cover, "kind": "track", "owner_url": owner_url,
        })
    if not items:
        return None
    return items, title + (f" · First 300 of {all_count}" if all_count > 300 else "")


class _Meta(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.data: dict[str, str] = {}
        self.next_data = ""
        self._next = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self._next = True
        if tag != "meta":
            return
        fields = dict(attrs)
        key = fields.get("property") or fields.get("name")
        if key and fields.get("content"):
            self.data[key] = fields["content"] or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._next = False

    def handle_data(self, data: str) -> None:
        if self._next:
            self.next_data += data


async def resolve_track(url: str) -> CatalogTrack | None:
    """Use documented public pages; return None when the link is not a track."""
    url = clean_url(url)
    provider = source(url)
    path = urlsplit(url).path
    if provider == "spotify" and not path.startswith("/track/"):
        return None
    if provider == "deezer" and "/track/" not in path:
        return None
    if provider == "apple" and (
        "music.apple.com" not in urlsplit(url).netloc or not parse_qs(urlsplit(url).query).get("i")
    ):
        return None
    if provider not in {"spotify", "deezer", "apple"}:
        return None
    validate_dns(url)
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        if provider == "apple":
            identifier = parse_qs(urlsplit(url).query)["i"][0]
            if not identifier.isdigit():
                return None
            response = await client.get(
                "https://itunes.apple.com/lookup", params={"id": identifier, "entity": "song"}
            )
            response.raise_for_status()
            results = response.json().get("results", [])
            data = next((x for x in results if x.get("trackId") == int(identifier)), None)
            if not data:
                raise ValueError("Apple Music track metadata unavailable")
            return CatalogTrack(
                data.get("trackName", ""),
                data.get("artistName", ""),
                url,
                int(data["trackTimeMillis"]) // 1000 if data.get("trackTimeMillis") else None,
                cover=str(data.get("artworkUrl100") or "").replace("100x100bb", "600x600bb"),
            )
        if provider == "deezer":
            identifier = path.rstrip("/").split("/")[-1]
            if not identifier.isdigit():
                return None
            response = await client.get(f"https://api.deezer.com/track/{identifier}")
            response.raise_for_status()
            data = response.json()
            if data.get("error") or not data.get("title"):
                raise ValueError("Deezer track metadata unavailable")
            return CatalogTrack(
                data["title"],
                (data.get("artist") or {}).get("name", ""),
                url,
                data.get("duration"),
                data.get("isrc"),
                str((data.get("album") or {}).get("cover_medium") or ""),
            )
        identifier = path.strip("/").split("/")[1]
        embed = await client.get(
            f"https://open.spotify.com/embed/track/{identifier}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if embed.status_code == 200:
            parser = _Meta()
            parser.feed(embed.text[:900_000])
            try:
                entity = json.loads(parser.next_data)["props"]["pageProps"]["state"]["data"][
                    "entity"
                ]
                artists = ", ".join(
                    str(x.get("name") or "") for x in entity.get("artists", []) if x.get("name")
                )
                if entity.get("title") and artists:
                    return CatalogTrack(
                        str(entity["title"]), artists, url,
                        int(entity["duration"]) // 1000 if entity.get("duration") else None,
                        cover=_spotify_art(entity),
                    )
            except (ValueError, KeyError, TypeError):
                pass
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        parser = _Meta()
        parser.feed(response.text[:600_000])
        title = parser.data.get("og:title", "").strip()
        description = parser.data.get("og:description", "")
        artist = description.split(" · ")[0].strip() if " · " in description else ""
        if not title:
            raise ValueError("Catalog track metadata unavailable")
        return CatalogTrack(title, artist, url)


async def resolve_collection(url: str) -> tuple[list[dict[str, str]], str] | None:
    """Browse bounded public album/playlist metadata without acquiring audio."""
    url = clean_url(url)
    provider = source(url)
    path = urlsplit(url).path.strip("/").split("/")
    if provider not in {"spotify", "deezer", "apple"}:
        return None
    if provider == "spotify" and (len(path) < 2 or path[0] not in {"album", "playlist", "show"}):
        return None
    if provider == "deezer" and (len(path) < 2 or path[-2] not in {"album", "playlist"}):
        return None
    if provider == "apple" and (
        "music.apple.com" not in urlsplit(url).netloc or "album" not in path
    ):
        return None
    validate_dns(url)
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        if provider == "spotify":
            kind, identifier = path[:2]
            if not identifier.isalnum():
                return None
            if kind == "show":
                return await _spotify_show_collection(client, identifier)
            try:
                complete = await _spotify_music_collection(client, kind, identifier)
                if complete is not None:
                    return complete
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                # Public embeds remain a catalog-metadata fallback when an app's
                # API entitlement is unavailable or a page request fails.
                pass
            response = await client.get(
                f"https://open.spotify.com/embed/{kind}/{identifier}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            parser = _Meta()
            parser.feed(response.text[:900_000])
            try:
                entity = json.loads(parser.next_data)["props"]["pageProps"]["state"]["data"][
                    "entity"
                ]
            except (ValueError, KeyError, TypeError):
                raise ValueError("Spotify collection metadata unavailable") from None
            items = [
                {
                    "title": str(x.get("title") or "Untitled")[:100],
                    "artist": str(x.get("subtitle") or entity.get("subtitle") or "")[:100],
                    "duration": int(x["duration"]) // 1000 if x.get("duration") else 0,
                    "url": "https://open.spotify.com/track/" + x["uri"].split(":")[-1],
                    "source_url": "https://open.spotify.com/track/" + x["uri"].split(":")[-1],
                    "cover": _spotify_art(entity) if kind == "album" else "",
                    "kind": "track",
                }
                for x in entity.get("trackList", [])[:300]
                if str(x.get("uri", "")).startswith("spotify:track:")
            ]
            return items, str(entity.get("title") or entity.get("name") or "Collection")
        if provider == "deezer":
            kind, identifier = path[-2:]
            if not identifier.isdigit():
                return None
            response = await client.get(f"https://api.deezer.com/{kind}/{identifier}")
            response.raise_for_status()
            data = response.json()
            tracks = (data.get("tracks") or {}).get("data", [])[:300]
            items = [
                {"title": str(x.get("title") or "Untitled")[:100], "url": x["link"],
                 "artist": str((x.get("artist") or {}).get("name") or ""),
                 "duration": x.get("duration") or 0,
                 "cover": str((x.get("album") or {}).get("cover_medium") or ""),
                 "kind": "track", "source_url": x["link"]}
                for x in tracks
                if x.get("link")
            ]
            return items, str(data.get("title") or "Collection")
        identifier = path[-1]
        if not identifier.isdigit():
            return None
        response = await client.get(
            "https://itunes.apple.com/lookup",
            params={"id": identifier, "entity": "song", "limit": 300},
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        items = [
            {"title": str(x.get("trackName") or "Untitled")[:100], "url": x["trackViewUrl"],
             "artist": str(x.get("artistName") or ""),
             "duration": int(x["trackTimeMillis"]) // 1000 if x.get("trackTimeMillis") else 0,
             "cover": str(x.get("artworkUrl100") or "").replace("100x100bb", "600x600bb"),
             "kind": "track", "source_url": x["trackViewUrl"]}
            for x in results
            if x.get("wrapperType") == "track" and x.get("trackViewUrl")
        ][:300]
        return items, str(results[0].get("collectionName") or "Album") if results else "Album"
