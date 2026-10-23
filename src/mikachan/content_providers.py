"""Source-backed content lookups. Provider failures never become invented items."""

import datetime as dt
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup


class ContentError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    title: str
    description: str
    source_url: str
    provider: str
    image_url: str = ""
    credit: str = ""
    license: str = ""
    date: str = ""
    identifier: str = ""
    audio_url: str = ""


class ContentProviders:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.client = httpx.AsyncClient(timeout=12, follow_redirects=False,
            headers={"User-Agent": "Mikachan/2.0 (https://github.com/Dneeel/Mikachan)"},
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            transport=transport)
        self._cache: dict[str, tuple[float, Any]] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def json(self, url: str, params: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> dict[str, Any] | list[Any]:
        return await self._fetch(url, params, "json", headers)

    async def text(self, url: str, params: dict[str, Any] | None = None) -> str:
        return await self._fetch(url, params, "text")

    async def _fetch(self, url: str, params: dict[str, Any] | None, kind: str,
                     headers: dict[str, str] | None = None) -> Any:
        host = urlsplit(url).hostname or ""
        if not url.startswith("https://") or host not in {
            "api.thecatapi.com", "cataas.com", "openaccess-api.clevelandart.org",
            "api.inaturalist.org", "en.wikipedia.org", "fa.wikipedia.org",
            "en.wikiquote.org", "fa.wikiquote.org", "en.wiktionary.org",
            "commons.wikimedia.org"}:
            raise ContentError("Content source is not approved")
        cacheable = (host not in {"api.thecatapi.com", "cataas.com"} and
                     not (params or {}).get("generator") == "random")
        key = json.dumps([url, params or {}, kind], sort_keys=True, ensure_ascii=True)
        stored = self._cache.get(key) if cacheable else None
        if stored and stored[0] > time.monotonic():
            return stored[1]
        try:
            async with self.client.stream("GET", url, params=params, headers=headers) as response:
                if response.status_code == 429:
                    raise ContentError("Content source rate limit reached; try later")
                if response.status_code in {401, 403}:
                    raise ContentError("Content source denied access")
                if response.status_code == 404:
                    raise ContentError("No source item is published for this selection")
                response.raise_for_status()
                media_type = response.headers.get("content-type", "")
                body = bytearray()
                async for chunk in response.aiter_bytes(65536):
                    body.extend(chunk)
                    if len(body) > 1_500_000:
                        raise ContentError("Content source response is too large")
            if kind == "json":
                if "json" not in media_type:
                    raise ContentError("Content source returned an unexpected format")
                result = json.loads(body)
            else:
                if "text/html" not in media_type and "text/plain" not in media_type:
                    raise ContentError("Content source returned an unexpected format")
                result = body.decode("utf-8", errors="replace")
            if cacheable:
                if len(self._cache) >= 64:
                    self._cache.pop(next(iter(self._cache)))
                ttl = 60 if (params or {}).get("page") == "Main Page" else 300
                self._cache[key] = (time.monotonic() + ttl, result)
            return result
        except json.JSONDecodeError as exc:
            raise ContentError("Content source returned malformed JSON") from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ContentError("Content source did not respond") from exc
        except httpx.HTTPStatusError as exc:
            raise ContentError(f"Content source failed (HTTP {exc.response.status_code})") from exc

    async def cat_candidate(self, provider: str, api_key: str = "") -> Item:
        if provider == "thecatapi":
            if not api_key:
                raise ContentError("TheCatAPI key is not configured")
            rows = await self.json("https://api.thecatapi.com/v1/images/search",
                                   {"limit": 1, "mime_types": "jpg,png"},
                                   {"x-api-key": api_key})
            if not isinstance(rows, list) or not rows:
                raise ContentError("TheCatAPI returned no candidate")
            row = rows[0]
            cat_id, url = str(row.get("id", "")), str(row.get("url", ""))
            if not re.fullmatch(r"[\w-]{3,80}", cat_id) or not url.startswith(
                    ("https://cdn2.thecatapi.com/images/", "https://cdn.thecatapi.com/images/")):
                raise ContentError("TheCatAPI returned an invalid image")
            return Item("Cat photo", "A cat photo from a public source.",
                        url, "TheCatAPI", url, identifier=cat_id)
        if provider == "cataas":
            row = await self.json("https://cataas.com/cat", {"json": "true"})
            if not isinstance(row, dict):
                raise ContentError("CATAAS returned no candidate")
            cat_id = str(row.get("id", ""))
            if not re.fullmatch(r"[\w-]{3,80}", cat_id):
                raise ContentError("CATAAS returned an invalid image ID")
            url = f"https://cataas.com/cat/{cat_id}"
            return Item("Cat photo", "A cat photo from a public source.",
                        url, "CATAAS", url, identifier=cat_id)
        raise ContentError("Unknown cat source")

    async def art_search(self, query: str, page: int = 0) -> tuple[list[Item], int]:
        data = await self.json("https://openaccess-api.clevelandart.org/api/artworks/",
            {"q": query[:100] or "painting", "has_image": 1, "limit": 8, "skip": page * 8})
        if not isinstance(data, dict):
            raise ContentError("Art catalog returned invalid data")
        total = int(data.get("info", {}).get("total", 0))
        items: list[Item] = []
        for row in data.get("data", []):
            if row.get("share_license_status") not in {"CC0", "CC-BY", "CC-BY-SA", "Public Domain"}:
                continue
            images = row.get("images") or {}
            image = (images.get("web") or {}).get("url", "")
            if not str(image).startswith("https://openaccess-cdn.clevelandart.org/"):
                continue
            artists = row.get("creators") or []
            artist = artists[0].get("description", "") if artists else ""
            fields = [artist, row.get("creation_date", ""), row.get("technique", "")]
            description = " · ".join(str(v) for v in fields if v)
            items.append(Item(str(row.get("title") or "Untitled")[:180], description[:700],
                              str(row.get("url") or ""), "Cleveland Museum of Art",
                              str(image), str(artist), str(row["share_license_status"]),
                              identifier=str(row.get("id", ""))))
        return items, total

    async def art_random(self, exclude_id: str = "") -> Item:
        initial, total = await self.art_search("painting")
        if not initial:
            raise ContentError("No rights-clear artwork is available")
        for _ in range(3):
            page = random.randint(0, min(200, max(0, (total - 1) // 8)))
            candidates = initial if page == 0 else (await self.art_search("painting", page))[0]
            eligible = [item for item in candidates if item.identifier != exclude_id]
            if eligible:
                return random.choice(eligible)
        raise ContentError("No different artwork was available")

    async def wiki_search(self, query: str, lang: str = "en", page: int = 0) -> tuple[list[str], int]:
        if lang not in {"en", "fa"}:
            raise ContentError("Unsupported Wikipedia language")
        data = await self.json(f"https://{lang}.wikipedia.org/w/api.php", {
            "action": "query", "list": "search", "srsearch": query[:100],
            "srlimit": 8, "sroffset": page * 8, "format": "json"})
        search = data.get("query", {}).get("search", []) if isinstance(data, dict) else []
        total = data.get("query", {}).get("searchinfo", {}).get("totalhits", 0) \
            if isinstance(data, dict) else 0
        return [str(row["title"]) for row in search if row.get("title")], int(total)

    async def wiki_article(self, title: str, lang: str = "en") -> Item:
        if lang not in {"en", "fa"}:
            raise ContentError("Unsupported Wikipedia language")
        data = await self.json(f"https://{lang}.wikipedia.org/w/api.php", {
            "action": "query", "prop": "extracts|pageimages", "titles": title[:200],
            "exintro": 1, "explaintext": 1, "exchars": 900, "pithumbsize": 640,
            "format": "json", "formatversion": 2})
        pages = data.get("query", {}).get("pages", []) if isinstance(data, dict) else []
        if not pages or pages[0].get("missing"):
            raise ContentError("Wikipedia has no article for this title")
        page = pages[0]
        image = str((page.get("thumbnail") or {}).get("source", ""))
        if urlsplit(image).hostname not in {"upload.wikimedia.org", "thumb.wikimedia.org"}:
            image = ""
        actual_title = str(page.get("title", title))
        return Item(actual_title, str(page.get("extract") or "No lead excerpt available")[:900],
                    f"https://{lang}.wikipedia.org/wiki/{quote(actual_title.replace(' ', '_'))}",
                    f"Wikipedia ({lang.upper()})", image, identifier=actual_title)

    async def wiki_random(self, lang: str = "en") -> Item:
        data = await self.json(f"https://{lang}.wikipedia.org/w/api.php", {
            "action": "query", "generator": "random", "grnnamespace": 0,
            "grnlimit": 1, "format": "json", "formatversion": 2})
        pages = data.get("query", {}).get("pages", []) if isinstance(data, dict) else []
        if not pages:
            raise ContentError("Wikipedia returned no random article")
        return await self.wiki_article(str(pages[0]["title"]), lang)

    async def wiki_images(self, title: str, lang: str = "en") -> list[Item]:
        if lang not in {"en", "fa"}:
            raise ContentError("Unsupported Wikipedia language")
        data = await self.json(f"https://{lang}.wikipedia.org/w/api.php", {
            "action": "query", "prop": "images", "titles": title[:150],
            "imlimit": 30, "format": "json", "formatversion": 2})
        pages = data.get("query", {}).get("pages", []) if isinstance(data, dict) else []
        filenames = [str(row["title"]) for page in pages for row in page.get("images", [])
                     if str(row.get("title", "")).lower().endswith((".jpg", ".jpeg", ".png"))]
        if not filenames:
            raise ContentError("This article has no supported images")
        info = await self.json("https://commons.wikimedia.org/w/api.php", {
            "action": "query", "prop": "imageinfo", "titles": "|".join(filenames[:20]),
            "iiprop": "url|extmetadata", "iiurlwidth": 640,
            "format": "json", "formatversion": 2})
        items = []
        for page in info.get("query", {}).get("pages", []) if isinstance(info, dict) else []:
            image = (page.get("imageinfo") or [{}])[0]
            source = str(image.get("descriptionurl") or "")
            preview = str(image.get("thumburl") or "")
            if not source.startswith("https://commons.wikimedia.org/wiki/File:") or \
                    urlsplit(preview).hostname not in {"upload.wikimedia.org", "thumb.wikimedia.org"}:
                continue
            metadata = image.get("extmetadata") or {}
            license_name = str((metadata.get("LicenseShortName") or {}).get("value") or "")
            artist = BeautifulSoup(str((metadata.get("Artist") or {}).get("value") or ""),
                                   "html.parser").get_text(" ", strip=True)
            items.append(Item(str(page.get("title", "")).removeprefix("File:"),
                              f"Image associated with {title}", source, "Wikimedia Commons",
                              preview, artist[:150], license_name[:80]))
        if not items:
            raise ContentError("No image with source metadata was available")
        return items[:20]

    async def wiki_sections(self, title: str, lang: str = "en") -> list[dict[str, str]]:
        if lang not in {"en", "fa"}:
            raise ContentError("Unsupported Wikipedia language")
        data = await self.json(f"https://{lang}.wikipedia.org/w/api.php", {
            "action": "parse", "page": title[:150], "prop": "sections", "format": "json"})
        rows = data.get("parse", {}).get("sections", []) if isinstance(data, dict) else []
        result = [{"index": str(row["index"]),
                   "title": BeautifulSoup(str(row.get("line") or ""), "html.parser")
                   .get_text(" ", strip=True)[:80]}
                  for row in rows if str(row.get("index", "")).isdigit()]
        if not result:
            raise ContentError("No readable article sections were found")
        return result[:20]

    async def wiki_section(self, title: str, section: str, lang: str = "en") -> str:
        if lang not in {"en", "fa"} or not section.isdigit():
            raise ContentError("Invalid Wikipedia section")
        data = await self.json(f"https://{lang}.wikipedia.org/w/api.php", {
            "action": "parse", "page": title[:150], "section": section,
            "prop": "text", "format": "json"})
        if not isinstance(data, dict) or "parse" not in data:
            raise ContentError("This article section is unavailable")
        soup = BeautifulSoup(data["parse"].get("text", {}).get("*", ""), "html.parser")
        for node in soup.select("script,style,sup.reference,.mw-editsection,.reference,.noprint"):
            node.decompose()
        body = soup.get_text(" ", strip=True)[:1500]
        if not body:
            raise ContentError("This article section has no readable text")
        return body

    async def quote_page(self, title: str, lang: str = "en") -> tuple[list[Item], str]:
        if lang not in {"en", "fa"}:
            raise ContentError("Unsupported Wikiquote language")
        data = await self.json(f"https://{lang}.wikiquote.org/w/api.php", {
            "action": "parse", "page": title[:150], "prop": "text", "format": "json"})
        if not isinstance(data, dict) or "parse" not in data:
            raise ContentError("Wikiquote page is unavailable")
        parsed = data["parse"]
        actual = str(parsed.get("title", title))
        source_url = f"https://{lang}.wikiquote.org/wiki/{quote(actual.replace(' ', '_'))}"
        soup = BeautifulSoup(parsed.get("text", {}).get("*", ""), "html.parser")
        for tag in soup.select("script,style,sup.reference,.mw-editsection, .reference, .noprint"):
            tag.decompose()
        items = []
        for node in soup.select("div.mw-parser-output > ul > li"):
            for nested in node.select("ul,ol"):
                nested.decompose()
            text = node.get_text(" ", strip=True)
            if 25 <= len(text) <= 280 and not text.startswith(("See also", "Retrieved from")):
                items.append(Item(actual, text, source_url, f"Wikiquote ({lang.upper()})",
                                  credit=f"From the {actual} Wikiquote page"))
        if not items:
            raise ContentError("No suitable sourced quote excerpts were found")
        return items[:80], source_url

    async def quote_search(self, query: str, lang: str = "en") -> list[str]:
        if lang not in {"en", "fa"}:
            raise ContentError("Unsupported Wikiquote language")
        data = await self.json(f"https://{lang}.wikiquote.org/w/api.php", {
            "action": "query", "list": "search", "srsearch": query[:100],
            "srlimit": 8, "format": "json"})
        return [str(row["title"]) for row in data.get("query", {}).get("search", [])] \
            if isinstance(data, dict) else []

    async def quote_random(self, lang: str = "en") -> list[Item]:
        data = await self.json(f"https://{lang}.wikiquote.org/w/api.php", {
            "action": "query", "generator": "random", "grnnamespace": 0,
            "grnlimit": 1, "format": "json", "formatversion": 2})
        pages = data.get("query", {}).get("pages", []) if isinstance(data, dict) else []
        if not pages:
            raise ContentError("Wikiquote returned no random page")
        return (await self.quote_page(str(pages[0]["title"]), lang))[0]

    async def species_candidates(self, query: str) -> list[dict[str, Any]]:
        found = await self.json("https://api.inaturalist.org/v1/taxa/autocomplete",
                                {"q": query[:100], "per_page": 5})
        rows = found.get("results", []) if isinstance(found, dict) else []
        return [{"id": int(t["id"]), "name": str(t.get("name") or ""),
                 "common": str(t.get("preferred_common_name") or "")}
                for t in rows if t.get("id")]

    async def species(self, query: str = "", taxon_id: int | None = None) -> Item:
        if taxon_id is not None:
            data = await self.json(f"https://api.inaturalist.org/v1/taxa/{taxon_id}")
            rows = data.get("results", []) if isinstance(data, dict) else []
            if not rows:
                raise ContentError("Selected taxon is no longer available")
            taxon = rows[0]
        elif not query:
            # The taxon ID comes from the real API's popular taxa results.
            popular = await self.json("https://api.inaturalist.org/v1/observations/species_counts",
                                      {"quality_grade": "research", "per_page": 30})
            rows = popular.get("results", []) if isinstance(popular, dict) else []
            taxa = [r.get("taxon") for r in rows if r.get("taxon", {}).get("id")]
            if not taxa:
                raise ContentError("iNaturalist returned no species")
            taxon = random.choice(taxa)
        else:
            candidates = await self.species_candidates(query)
            if not candidates:
                raise ContentError("No matching species was found")
            taxon = {"id": candidates[0]["id"], "name": candidates[0]["name"],
                     "preferred_common_name": candidates[0]["common"]}
        taxon_id = int(taxon["id"])
        found = await self.json("https://api.inaturalist.org/v1/observations", {
            "taxon_id": taxon_id, "photos": "true", "quality_grade": "research",
            "per_page": 20, "order_by": "observed_on"})
        rows = found.get("results", []) if isinstance(found, dict) else []
        rows = [r for r in rows if r.get("photos") and r.get("id")]
        if not rows:
            raise ContentError("No licensed public observation photo was found")
        eligible = [(r, p) for r in rows for p in r["photos"] if p.get("license_code") in
                    {"cc0", "cc-by", "cc-by-sa", "cc-by-nc", "cc-by-nc-sa"}]
        if not eligible:
            raise ContentError("The observation photo has no eligible license metadata")
        observation, photo = random.choice(eligible)
        photo_url = str(photo.get("url", "")).replace("square", "medium")
        if urlsplit(photo_url).hostname not in {"inaturalist-open-data.s3.amazonaws.com",
              "static.inaturalist.org", "images.inaturalist.org", "inaturalist-open-data.s3.us-west-2.amazonaws.com"}:
            photo_url = ""
        scientific = str(taxon.get("name") or "")
        common = str(taxon.get("preferred_common_name") or scientific)
        place = str(observation.get("place_guess") or "place not recorded")
        date = str(observation.get("observed_on") or "date not recorded")
        return Item(common, f"{scientific}\nObserved {date} · {place}",
                    f"https://www.inaturalist.org/observations/{observation['id']}",
                    "iNaturalist", photo_url, str(photo.get("attribution") or ""),
                    str(photo.get("license_code") or ""), date, str(observation["id"]))

    async def nature(self, query: str = "") -> Item:
        # Wikipedia provides a factual lead. The photograph, when present, is
        # its actual page image and is explicitly credited to Wikimedia.
        term = query.strip() or random.choice(("Aurora", "Volcano", "Glacier", "Waterfall"))
        matches, _ = await self.wiki_search(term)
        if not matches:
            raise ContentError("No source-backed nature topic was found")
        item = await self.wiki_article(matches[0])
        return Item(item.title, item.description, item.source_url,
                    "Wikipedia / Wikimedia", item.image_url, "Wikimedia page image")

    async def daily(self, topic: str, day: dt.date) -> list[Item]:
        date = day.isoformat()
        if topic in {"picture", "media"}:
            stem = "Potd" if topic == "picture" else "Motd"
            page = f"Template:{stem}/{date}"
            data = await self.json("https://commons.wikimedia.org/w/api.php", {
                "action": "parse", "page": page, "prop": "text", "format": "json"})
            if not isinstance(data, dict) or "parse" not in data:
                raise ContentError("Commons has not published this daily item")
            soup = BeautifulSoup(data["parse"].get("text", {}).get("*", ""), "html.parser")
            link = next((a for a in soup.select('a[href^="/wiki/File:"]') if
                         a.get_text(" ", strip=True).startswith("File:")), None)
            if not link:
                raise ContentError("Commons daily media has no verified file link")
            file_title = link.get_text(" ", strip=True)
            metadata_response = await self.json("https://commons.wikimedia.org/w/api.php", {
                "action": "query", "prop": "imageinfo", "titles": file_title,
                "iiprop": "url|extmetadata", "iiurlwidth": 800,
                "format": "json", "formatversion": 2})
            file_pages = metadata_response.get("query", {}).get("pages", []) \
                if isinstance(metadata_response, dict) else []
            file_info = (file_pages[0].get("imageinfo") or [{}])[0] if file_pages else {}
            metadata = file_info.get("extmetadata") or {}
            preview = str(file_info.get("thumburl") or "") if topic == "picture" else ""
            if urlsplit(preview).hostname not in {"upload.wikimedia.org", "thumb.wikimedia.org"}:
                preview = ""
            license_name = str((metadata.get("LicenseShortName") or {}).get("value") or "")
            creator = BeautifulSoup(str((metadata.get("Artist") or {}).get("value") or ""),
                                    "html.parser").get_text(" ", strip=True)
            description = BeautifulSoup(str((metadata.get("ImageDescription") or {}).get("value")
                or ""), "html.parser").get_text(" ", strip=True)[:600]
            title = file_title.removeprefix("File:")
            return [Item(title, description or soup.get_text(" ", strip=True)[:600],
                urljoin("https://commons.wikimedia.org", str(link["href"])),
                "Wikimedia Commons", preview, creator[:150], license_name[:80], date=date)]
        if topic == "word":
            page = f"Wiktionary:Word_of_the_day/{day.year}/{day.strftime('%B')}_{day.day}"
            data = await self.json("https://en.wiktionary.org/w/api.php", {
                "action": "parse", "page": page, "prop": "text", "format": "json"})
            if not isinstance(data, dict) or "parse" not in data:
                raise ContentError("Wiktionary has not published this word")
            soup = BeautifulSoup(data["parse"].get("text", {}).get("*", ""), "html.parser")
            link = next((a for a in soup.select('a[href^="/wiki/"]') if
                         a.get_text(" ", strip=True) and "Word_of_the_day" not in a["href"]), None)
            if not link:
                raise ContentError("Wiktionary did not expose a word entry")
            entry_url = urljoin("https://en.wiktionary.org", str(link["href"]))
            try:
                entry = await self.json("https://en.wiktionary.org/w/api.php", {
                    "action": "parse", "page": link.get_text(" ", strip=True),
                    "prop": "text", "format": "json"})
            except ContentError:
                entry = {}
            entry_html = entry.get("parse", {}).get("text", {}).get("*", "") \
                if isinstance(entry, dict) else ""
            entry_soup = BeautifulSoup(entry_html, "html.parser")
            audio = next((s for s in entry_soup.select("audio source[src]") if
                          str(s.get("src", "")).split("?", 1)[0].endswith(".mp3")), None)
            audio_url = ("https:" + str(audio["src"]) if audio and
                str(audio["src"]).startswith("//upload.wikimedia.org/") else "")
            return [Item(link.get_text(" ", strip=True), soup.get_text(" ", strip=True)[:800],
                entry_url, "Wiktionary", date=date, audio_url=audio_url)]
        data = await self.json(f"https://en.wikipedia.org/api/rest_v1/feed/featured/"
                               f"{day.year}/{day.month:02}/{day.day:02}")
        if not isinstance(data, dict):
            raise ContentError("Wikipedia daily feed is invalid")
        if topic == "featured":
            article = data.get("tfa") or {}
            if not article.get("title"):
                raise ContentError("No featured article is published for this day")
            return [Item(str(article["title"]).replace("_", " "),
                str(article.get("extract") or "")[:900],
                str(article.get("content_urls", {}).get("desktop", {}).get("page") or ""),
                "Wikipedia", str((article.get("thumbnail") or {}).get("source") or ""), date=date)]
        if topic == "mostread":
            bucket = data.get("mostread") or {}
            actual_date = str(bucket.get("date") or date).rstrip("Z")
            return [Item(str(a.get("title", "")).replace("_", " "),
                f"Rank {a.get('rank', '?')} · {a.get('views', '?')} views",
                str(a.get("content_urls", {}).get("desktop", {}).get("page") or ""),
                "Wikipedia pageviews", date=actual_date)
                for a in bucket.get("articles", [])[:30] if a.get("title")]
        if topic in {"events", "births", "deaths"}:
            if topic == "events":
                rows = data.get("onthisday") or []
            else:
                extra = await self.json(f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/"
                    f"{topic}/{day.month:02}/{day.day:02}")
                rows = extra.get(topic) or [] if isinstance(extra, dict) else []
            return [Item(f"{r.get('year', '?')} · {str(r.get('text', ''))[:120]}",
                str(r.get("text") or "")[:700],
                str(((r.get("pages") or [{}])[0].get("content_urls") or {}).get("desktop", {}).get("page")
                    or "https://en.wikipedia.org/wiki/Main_Page"),
                "Wikipedia On This Day", date=date) for r in rows[:35] if r.get("text")]
        if topic in {"dyk", "news"}:
            if day != dt.datetime.now(dt.UTC).date():
                raise ContentError("Historical editorial items are unavailable for this date")
            main_page = await self.json("https://en.wikipedia.org/w/api.php", {
                "action": "parse", "page": "Main Page", "prop": "text", "format": "json"})
            if not isinstance(main_page, dict):
                raise ContentError("Wikipedia main page is unavailable")
            soup = BeautifulSoup(main_page.get("parse", {}).get("text", {}).get("*", ""),
                                 "html.parser")
            node = soup.select_one("#mp-dyk" if topic == "dyk" else "#mp-itn")
            if not node:
                raise ContentError("Wikipedia editorial section is unavailable")
            items = []
            for li in node.select("li")[:20]:
                text = li.get_text(" ", strip=True)
                link = li.select_one('a[href^="/wiki/"]')
                if text and link:
                    items.append(Item("Did you know?" if topic == "dyk" else "In the News",
                        text[:500], urljoin("https://en.wikipedia.org", str(link["href"])),
                        "Wikipedia main page", date=f"retrieved {dt.date.today().isoformat()}"))
            return items
        raise ContentError("Unsupported daily topic")
