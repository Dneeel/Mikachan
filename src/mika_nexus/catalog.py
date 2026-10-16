"""Resolve public catalog links to a track identity; no protected audio is fetched."""

from dataclasses import dataclass
from html.parser import HTMLParser
import json
from urllib.parse import parse_qs, urlsplit

import httpx

from .security import clean_url, source, validate_dns


@dataclass(frozen=True)
class CatalogTrack:
    title: str
    artist: str
    url: str
    duration: int | None = None
    isrc: str | None = None


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
            )
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
    if provider == "spotify" and (len(path) < 2 or path[0] not in {"album", "playlist"}):
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
                    "url": "https://open.spotify.com/track/" + x["uri"].split(":")[-1],
                }
                for x in entity.get("trackList", [])[:30]
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
            tracks = (data.get("tracks") or {}).get("data", [])[:30]
            items = [
                {"title": str(x.get("title") or "Untitled")[:100], "url": x["link"]}
                for x in tracks
                if x.get("link")
            ]
            return items, str(data.get("title") or "Collection")
        identifier = path[-1]
        if not identifier.isdigit():
            return None
        response = await client.get(
            "https://itunes.apple.com/lookup",
            params={"id": identifier, "entity": "song", "limit": 30},
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        items = [
            {"title": str(x.get("trackName") or "Untitled")[:100], "url": x["trackViewUrl"]}
            for x in results
            if x.get("wrapperType") == "track" and x.get("trackViewUrl")
        ][:30]
        return items, str(results[0].get("collectionName") or "Album") if results else "Album"
