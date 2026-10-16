"""Federated catalog metadata; never downloads media while searching."""

import asyncio
import json
import os
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

import httpx

KINDS = ("track", "album", "playlist", "artist", "podcast", "episode")
SOURCES = ("apple", "deezer", "soundcloud", "ytmusic", "youtube", "spotify", "apple_podcasts")


def _terms(value: str) -> set[str]:
    value = unicodedata.normalize("NFKC", value).casefold().replace("\u200c", " ")
    return set(re.findall(r"[^\W_]+", value, flags=re.UNICODE))


def _relevance(query: str, hit: "Hit") -> float:
    wanted = _terms(query)
    title = _terms(hit.title)
    combined = title | _terms(hit.creator)
    coverage = len(wanted & combined) / len(wanted) if wanted else 0.0
    exact = 0.5 if wanted == title else 0.0
    return coverage + exact


@dataclass(frozen=True)
class Hit:
    source: str
    kind: str
    title: str
    creator: str
    url: str
    id: str
    downloadable: bool = False
    duration: int | None = None
    isrc: str | None = None

    def data(self) -> dict[str, Any]:
        return asdict(self)


class Searcher:
    def __init__(self) -> None:
        self.health: dict[str, str] = {x: "unverified" for x in SOURCES}
        self.semaphore = asyncio.Semaphore(3)
        if not (os.getenv("SPOTIFY_CLIENT_ID") and os.getenv("SPOTIFY_CLIENT_SECRET")):
            self.health["spotify"] = "credentials required"

    async def search(
        self, query: str, kinds: set[str], sources: set[str]
    ) -> tuple[list[Hit], dict[str, str]]:
        if not kinds or not sources:
            return [], {}
        tasks = [
            (s, asyncio.create_task(self._one(s, query, kinds))) for s in SOURCES if s in sources
        ]
        results: list[Hit] = []
        failures: dict[str, str] = {}
        done, pending = await asyncio.wait([task for _, task in tasks], timeout=12)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for source, task in tasks:
            try:
                if task not in done:
                    raise TimeoutError()
                hits = task.result()
                results.extend(h for h in hits if h.source == source and h.kind in kinds)
                self.health[source] = "ok"
            except Exception as exc:
                failures[source] = (
                    str(exc)[:80] if isinstance(exc, RuntimeError) else type(exc).__name__
                )
                self.health[source] = failures[source]
        # Only exact source-id duplicates collapse. Distinct versions and uploads survive.
        seen: set[tuple[str, str, str]] = set()
        unique: list[Hit] = []
        for h in results:
            key = (h.source, h.kind, h.id)
            if key not in seen:
                unique.append(h)
                seen.add(key)
        # Round-robin across providers gives multiple selected sources visibility.
        buckets = {s: [h for h in unique if h.source == s] for s in SOURCES}
        ordered: list[Hit] = []
        while any(buckets.values()):
            for s in SOURCES:
                if buckets[s]:
                    ordered.append(buckets[s].pop(0))
        if kinds <= {"podcast", "episode"}:
            preference = {"apple_podcasts": 0.15, "spotify": 0.12, "ytmusic": 0.04}
            ordered.sort(
                key=lambda h: (
                    _relevance(query, h),
                    0.25 if h.kind == "podcast" else 0.0,
                    preference.get(h.source, 0.0),
                ),
                reverse=True,
            )
            deduped: list[Hit] = []
            program_keys: set[tuple[str, str]] = set()
            for hit in ordered:
                program_key = (" ".join(sorted(_terms(hit.title))), " ".join(sorted(_terms(hit.creator))))
                if hit.kind == "podcast" and program_key in program_keys:
                    continue
                if hit.kind == "podcast":
                    program_keys.add(program_key)
                deduped.append(hit)
            ordered = deduped
        else:
            ordered.sort(key=lambda h: _relevance(query, h), reverse=True)
        return ordered[:50], failures

    async def _one(self, source: str, query: str, kinds: set[str]) -> list[Hit]:
        fn = getattr(self, "_" + source)
        async with self.semaphore:
            return await fn(query, kinds)

    async def _apple(self, query: str, kinds: set[str]) -> list[Hit]:
        types = {"track": "song", "album": "album", "artist": "musicArtist"}
        out: list[Hit] = []
        async with httpx.AsyncClient(timeout=8) as client:
            for kind, entity in types.items():
                if kind not in kinds:
                    continue
                r = await client.get(
                    "https://itunes.apple.com/search",
                    params={"term": query, "entity": entity, "limit": 15},
                )
                r.raise_for_status()
                for x in r.json().get("results", []):
                    title = x.get("trackName") or x.get("collectionName") or x.get("artistName")
                    url = (
                        x.get("trackViewUrl")
                        or x.get("collectionViewUrl")
                        or x.get("artistViewUrl")
                    )
                    if title and url:
                        out.append(
                            Hit(
                                "apple",
                                kind,
                                title,
                                x.get("artistName", ""),
                                url,
                                str(x.get("trackId") or x.get("collectionId") or x.get("artistId")),
                                False,
                                (int(x["trackTimeMillis"]) // 1000)
                                if x.get("trackTimeMillis")
                                else None,
                            )
                        )
        return out

    async def _apple_podcasts(self, query: str, kinds: set[str]) -> list[Hit]:
        out: list[Hit] = []
        async with httpx.AsyncClient(timeout=8) as client:
            for kind, entity in (("podcast", "podcast"), ("episode", "podcastEpisode")):
                if kind not in kinds:
                    continue
                r = await client.get(
                    "https://itunes.apple.com/search",
                    params={"term": query, "entity": entity, "limit": 15},
                )
                r.raise_for_status()
                for x in r.json().get("results", []):
                    title = x.get("trackName") or x.get("collectionName")
                    url = x.get("trackViewUrl") or x.get("collectionViewUrl")
                    if title and url:
                        out.append(
                            Hit(
                                "apple_podcasts",
                                kind,
                                title,
                                x.get("artistName") or x.get("collectionName") or "",
                                url,
                                str(x.get("trackId") or x.get("collectionId")),
                                kind == "episode" and bool(x.get("episodeUrl")),
                            )
                        )
        return out

    async def _deezer(self, query: str, kinds: set[str]) -> list[Hit]:
        mapping = {"track": "track", "album": "album", "artist": "artist", "playlist": "playlist"}
        out: list[Hit] = []
        async with httpx.AsyncClient(timeout=8) as client:
            for kind, path in mapping.items():
                if kind not in kinds:
                    continue
                r = await client.get(
                    f"https://api.deezer.com/search/{path}", params={"q": query, "limit": 15}
                )
                r.raise_for_status()
                for x in r.json().get("data", []):
                    title = x.get("title") or x.get("name")
                    if title and x.get("link"):
                        out.append(
                            Hit(
                                "deezer",
                                kind,
                                title,
                                (x.get("artist") or {}).get("name", ""),
                                x["link"],
                                str(x["id"]),
                                False,
                                x.get("duration") if kind == "track" else None,
                                x.get("isrc"),
                            )
                        )
        return out

    async def _soundcloud(self, query: str, kinds: set[str]) -> list[Hit]:
        if "track" not in kinds:
            return []
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m", "yt_dlp",
            "--flat-playlist",
            "--dump-single-json",
            "--no-warnings",
            "--skip-download",
            f"scsearch15:{query}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", "")},
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), 10)
        except (TimeoutError, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            raise RuntimeError("SoundCloud public search unavailable")
        obj = json.loads(stdout)
        return [
            Hit(
                "soundcloud",
                "track",
                x.get("title", "Unknown"),
                x.get("uploader", ""),
                x.get("url") or x.get("webpage_url", ""),
                str(x.get("id")),
                True,
                x.get("duration"),
            )
            for x in obj.get("entries", [])
            if x and (x.get("url") or x.get("webpage_url")) and x.get("id")
        ]

    async def _youtube(self, query: str, kinds: set[str]) -> list[Hit]:
        """General video search catches recordings omitted from YouTube Music's song index."""
        if "track" not in kinds:
            return []
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "yt_dlp", "--flat-playlist", "--dump-single-json",
            "--no-warnings", "--skip-download", f"ytsearch12:{query}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", "")},
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), 10)
        except (TimeoutError, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            raise RuntimeError("YouTube public search unavailable")
        obj = json.loads(stdout)
        return [
            Hit("youtube", "track", str(x.get("title") or ""),
                str(x.get("channel") or x.get("uploader") or ""),
                str(x.get("url") or ""), str(x.get("id") or ""), True,
                x.get("duration"))
            for x in obj.get("entries", [])
            if x and x.get("id") and x.get("title") and x.get("url")
        ]

    async def _ytmusic(self, query: str, kinds: set[str]) -> list[Hit]:
        from ytmusicapi import YTMusic

        mapping = {
            "track": "songs",
            "album": "albums",
            "artist": "artists",
            "playlist": "playlists",
            "podcast": "podcasts",
            "episode": "episodes",
        }
        out: list[Hit] = []
        client = YTMusic()
        for kind, filt in mapping.items():
            if kind not in kinds:
                continue
            items = await asyncio.to_thread(client.search, query, filter=filt, limit=15)
            for x in items:
                id_ = x.get("videoId") or x.get("browseId")
                if id_ and x.get("title"):
                    url = (
                        "https://music.youtube.com/watch?v=" + id_
                        if x.get("videoId")
                        else "https://music.youtube.com/browse/" + id_
                    )
                    artists = x.get("artists") or []
                    out.append(
                        Hit(
                            "ytmusic",
                            kind,
                            x["title"],
                            ", ".join(a.get("name", "") for a in artists),
                            url,
                            id_,
                            kind in {"track", "episode"},
                            x.get("duration_seconds"),
                        )
                    )
        return out

    async def _spotify(self, query: str, kinds: set[str]) -> list[Hit]:
        import base64

        cid, secret = os.getenv("SPOTIFY_CLIENT_ID"), os.getenv("SPOTIFY_CLIENT_SECRET")
        if not cid or not secret:
            raise RuntimeError("Spotify credentials required")
        mapping = {"track": "track", "album": "album", "artist": "artist", "playlist": "playlist"}
        active = [v for k, v in mapping.items() if k in kinds]
        if not active:
            return []
        async with httpx.AsyncClient(timeout=8) as client:
            auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
            r = await client.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                headers={"Authorization": "Basic " + auth},
            )
            r.raise_for_status()
            token = r.json()["access_token"]
            r = await client.get(
                "https://api.spotify.com/v1/search",
                params={"q": query, "type": ",".join(active), "limit": 15},
                headers={"Authorization": "Bearer " + token},
            )
            r.raise_for_status()
            data = r.json()
        out: list[Hit] = []
        for kind, value in mapping.items():
            for x in data.get(value + "s", {}).get("items", []):
                if x and x.get("id") and x.get("external_urls", {}).get("spotify"):
                    artists = x.get("artists") or []
                    out.append(
                        Hit(
                            "spotify",
                            kind,
                            x["name"],
                            ", ".join(a.get("name", "") for a in artists),
                            x["external_urls"]["spotify"],
                            x["id"],
                        )
                    )
        return out
