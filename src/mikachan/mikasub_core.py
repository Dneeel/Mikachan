"""MikaSub's bounded bot-side build client for the existing Cloudflare resources."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass

import httpx

from .config import Settings
from .mikasub_codec import Node, Parsed, parse_subscription, public_name, rename
from .mikasub_fetch import fetch_subscription, source_url


class MikaSubError(ValueError):
    pass


@dataclass(frozen=True)
class BuildResult:
    version: str
    total: int
    raw: int
    parsed: int
    duplicates: int
    unsupported: int
    errors: int


class MikaSubCore:
    def __init__(self, settings: Settings, db: sqlite3.Connection):
        self.origin = settings.mikasub_worker_url.rstrip("/")
        self.admin_token = settings.mikasub_admin_token
        self.configured_public_url = settings.mikasub_public_url
        self.db = db
        self.build_lock = asyncio.Lock()
        self.background: asyncio.Task[None] | None = None
        self.last_status = "Idle"
        db.executescript("""
        CREATE TABLE IF NOT EXISTS mikasub_local (
          key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS mikasub_sources (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1, last_error TEXT NOT NULL DEFAULT '',
          last_ok TEXT NOT NULL DEFAULT '', raw_count INTEGER NOT NULL DEFAULT 0,
          valid_count INTEGER NOT NULL DEFAULT 0, exported_count INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS mikasub_node_ids (
          identity TEXT PRIMARY KEY, node_id INTEGER UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS mikasub_snapshots (
          version TEXT PRIMARY KEY, created_at TEXT NOT NULL,
          items_json TEXT NOT NULL, stats_json TEXT NOT NULL);
        """)
        self.local_override = bool(db.execute("SELECT 1 FROM mikasub_local "
            "WHERE key='backend' AND value='local'").fetchone())

    @property
    def configured(self) -> bool:
        return bool(self.origin and self.admin_token and not self.local_override)

    async def use_local(self) -> int:
        """Keep existing Worker data, copy accessible sources, then switch locally."""
        if not self.configured:
            return 0
        imported = 0
        try:
            previous = await self.sources()
        except (MikaSubError, OSError, asyncio.TimeoutError):
            previous = []
        with self.db:
            for item in previous[:50]:
                try:
                    url = source_url(str(item.get("url") or ""))
                except ValueError:
                    continue
                if self.db.execute("SELECT 1 FROM mikasub_sources WHERE url=?",
                                   (url,)).fetchone():
                    continue
                self.db.execute("INSERT INTO mikasub_sources(name,url,enabled) "
                    "VALUES (?,?,?)", (str(item.get("name") or "Source")[:80], url,
                                      int(bool(item.get("enabled")))))
                imported += 1
            self.db.execute("INSERT OR REPLACE INTO mikasub_local(key,value) "
                            "VALUES ('backend','local')")
        self.local_override = True
        return imported

    def link(self) -> str:
        if not self.configured:
            return ""
        row = self.db.execute("SELECT value FROM mikasub_local WHERE key='public_link'").fetchone()
        return str(row[0]) if row else self.configured_public_url

    async def api(self, method: str, path: str, *, data: object | None = None,
                  text: str | None = None, timeout: float = 25) -> dict:
        if not self.configured:
            match = re.fullmatch(r"/api/v1/sources/(\d+)", path)
            if not match or method not in {"PATCH", "DELETE"}:
                raise MikaSubError("This action is unavailable in local MikaSub mode")
            ident = int(match[1])
            if method == "DELETE":
                self.db.execute("DELETE FROM mikasub_sources WHERE id=?", (ident,))
            elif isinstance(data, dict):
                allowed = {"name", "url", "enabled", "last_error", "last_ok",
                           "raw_count", "valid_count", "exported_count"}
                updates = {key: value for key, value in data.items() if key in allowed}
                if not updates:
                    raise MikaSubError("No source fields to update")
                if "url" in updates:
                    source_url(str(updates["url"]))
                if "name" in updates and not 1 <= len(str(updates["name"])) <= 80:
                    raise MikaSubError("Source name must be 1–80 characters")
                self.db.execute("UPDATE mikasub_sources SET " + ",".join(
                    f"{key}=?" for key in updates) + " WHERE id=?",
                    (*updates.values(), ident))
            return {}
        if not path.startswith("/api/v1/") or "?" in path and "\n" in path:
            raise MikaSubError("invalid Worker API path")
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                         trust_env=False) as client:
                response = await client.request(method, self.origin + path,
                    headers=headers, json=data if data is not None else None,
                    content=text.encode("utf-8") if text is not None else None)
                if response.status_code >= 400:
                    try:
                        reason = response.json().get("error", "Worker request failed")
                    except ValueError:
                        reason = "Worker request failed"
                    raise MikaSubError(f"Worker HTTP {response.status_code}: {str(reason)[:120]}")
                result = response.json()
                if not isinstance(result, dict):
                    raise MikaSubError("Worker returned invalid JSON")
                return result
        except httpx.HTTPError as exc:
            raise MikaSubError("MikaSub Worker is temporarily unreachable") from exc

    async def sources(self) -> list[dict]:
        if not self.configured:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM mikasub_sources ORDER BY id")]
        value = (await self.api("GET", "/api/v1/sources")).get("sources")
        if not isinstance(value, list):
            raise MikaSubError("Worker source list is invalid")
        return value

    async def publication(self) -> dict:
        if not self.configured:
            row = self.db.execute("SELECT version,created_at,items_json,stats_json "
                "FROM mikasub_snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
            return {"ready": bool(row), "version": row[0] if row else "",
                "total": len(json.loads(row[2])) if row else 0,
                "published_at": row[1] if row else "",
                "stats": json.loads(row[3]) if row else {}}
        return await self.api("GET", "/api/v1/publication")

    async def metadata(self, version: str = "") -> dict:
        if not self.configured:
            row = self.db.execute("SELECT version,created_at,items_json FROM mikasub_snapshots "
                + ("WHERE version=?" if version else "ORDER BY rowid DESC LIMIT 1"),
                (version,) if version else ()).fetchone()
            if not row:
                raise MikaSubError("snapshot unavailable; Owner can add a source and Update Now")
            return {"version": row[0], "created_at": row[1],
                    "total": len(json.loads(row[2]))}
        suffix = "?version=" + version if version else ""
        return await self.api("GET", "/api/v1/snapshot/metadata" + suffix)

    async def items(self, version: str, offset: int, limit: int = 5) -> list[dict]:
        if not 0 <= offset <= 5000 or not 1 <= limit <= 100:
            raise MikaSubError("invalid snapshot page")
        if not self.configured:
            row = self.db.execute("SELECT items_json FROM mikasub_snapshots "
                                  "WHERE version=?", (version,)).fetchone()
            if not row:
                raise MikaSubError("snapshot version is unavailable")
            return json.loads(row[0])[offset:offset + limit]
        response = await self.api("GET", f"/api/v1/snapshot/items?version={version}&offset={offset}&limit={limit}")
        if response.get("version") != version or not isinstance(response.get("items"), list):
            raise MikaSubError("snapshot version changed unexpectedly")
        return response["items"]

    async def all_items(self, version: str, total: int) -> list[dict]:
        if not 0 <= total <= 5000:
            raise MikaSubError("snapshot size invalid")
        result = []
        for offset in range(0, total, 100):
            batch = await self.items(version, offset, min(100, total - offset))
            if len(batch) != min(100, total - offset):
                raise MikaSubError("snapshot items are incomplete")
            result.extend(batch)
        return result

    async def add_source(self, name: str, url: str) -> dict:
        source_url(url)
        # Validate the actual document before storing an access-bearing URL.
        parsed = parse_subscription(await fetch_subscription(url))
        if not parsed.nodes:
            raise MikaSubError("source has no losslessly exportable share URIs")
        if not self.configured:
            if not 1 <= len(name.strip()) <= 80 or len(await self.sources()) >= 50:
                raise MikaSubError("Source name or 50-source limit is invalid")
            cursor = self.db.execute("INSERT INTO mikasub_sources(name,url) VALUES (?,?)",
                                     (name.strip(), url))
            ident = int(cursor.lastrowid or 0)
            await self.source_stats(ident, parsed)
            return {"id": ident}
        created = await self.api("POST", "/api/v1/sources", data={"name": name, "url": url})
        await self.source_stats(int(created["id"]), parsed)
        return created

    async def replace_source(self, ident: int, url: str) -> None:
        source_url(url)
        parsed = parse_subscription(await fetch_subscription(url))
        if not parsed.nodes:
            raise MikaSubError("replacement has no losslessly exportable share URIs")
        if not self.configured and not self.db.execute(
                "SELECT 1 FROM mikasub_sources WHERE id=?", (ident,)).fetchone():
            raise MikaSubError("source not found")
        await self.api("PATCH", f"/api/v1/sources/{ident}", data={"url": url})
        await self.source_stats(ident, parsed)

    async def source_stats(self, ident: int, parsed: Parsed | None,
                           error: str = "") -> None:
        changes: dict[str, object] = {"last_error": error[:120]}
        if parsed is not None:
            changes.update({"last_ok": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "raw_count": parsed.raw, "valid_count": parsed.parsed,
                            "exported_count": len(parsed.nodes)})
        await self.api("PATCH", f"/api/v1/sources/{ident}", data=changes)

    async def fetch_one(self, ident: int) -> Parsed:
        row = next((source for source in await self.sources() if int(source["id"]) == ident), None)
        if not row:
            raise MikaSubError("source not found")
        try:
            parsed = parse_subscription(await fetch_subscription(str(row["url"])))
            if not parsed.nodes:
                raise MikaSubError("source has no exportable share URIs")
        except (ValueError, OSError, asyncio.TimeoutError) as exc:
            await self.source_stats(ident, None, str(exc)[:120])
            raise MikaSubError("source fetch or format failed; published snapshot unchanged") from exc
        await self.source_stats(ident, parsed)
        return parsed

    async def rotate_link(self) -> str:
        if not self.configured:
            raise MikaSubError("Local MikaSub is delivered inside Telegram; no public link exists")
        value = await self.api("POST", "/api/v1/public-token/rotate", data={})
        link = str(value.get("url") or "")
        if not link.startswith(self.origin + "/sub/") or len(link) < len(self.origin) + 50:
            raise MikaSubError("Worker returned an invalid subscription URL")
        self.db.execute("INSERT INTO mikasub_local VALUES ('public_link',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (link,))
        return link

    async def _ids(self, nodes: list[Node]) -> dict[str, int]:
        if not self.configured:
            for node in nodes:
                self.db.execute("INSERT OR IGNORE INTO mikasub_node_ids(identity,node_id) "
                    "VALUES (?,(SELECT COALESCE(MAX(node_id),0)+1 FROM mikasub_node_ids))",
                    (node.identity,))
            return {str(row[0]): int(row[1]) for row in self.db.execute(
                "SELECT identity,node_id FROM mikasub_node_ids")}
        found: dict[str, int] = {}
        for offset in range(0, len(nodes), 100):
            hashes = [node.identity for node in nodes[offset:offset + 100]]
            response = await self.api("POST", "/api/v1/node-ids", data={"hashes": hashes})
            ids = response.get("ids")
            if not isinstance(ids, dict) or set(ids) != set(hashes):
                raise MikaSubError("stable node IDs are incomplete")
            found.update({key: int(value) for key, value in ids.items()})
        return found

    async def rebuild(self) -> BuildResult:
        if self.build_lock.locked():
            raise MikaSubError("a MikaSub update is already running")
        async with self.build_lock:
            self.last_status = "Fetching subscription documents"
            try:
                result = await self._rebuild()
                self.last_status = f"Published {result.total} configurations"
                return result
            except Exception:
                self.last_status = "Update failed; last published collection retained"
                raise

    async def _rebuild(self) -> BuildResult:
        if not self.configured:
            return await self._rebuild_local()
        sources = [item for item in await self.sources() if item.get("enabled")]
        if not sources:
            raise MikaSubError("add and enable at least one source first")
        if len(sources) > 50:
            raise MikaSubError("more than 50 sources are enabled")
        gate = asyncio.Semaphore(3)
        budget_lock = asyncio.Lock()
        total_source_bytes = 0

        async def collect(item: dict) -> tuple[dict, Parsed | None, str]:
            nonlocal total_source_bytes
            async with gate:
                try:
                    document = await fetch_subscription(str(item["url"]))
                    async with budget_lock:
                        total_source_bytes += len(document)
                        if total_source_bytes > 8_000_000:
                            raise MikaSubError("combined source documents exceed 8 MB")
                    parsed = parse_subscription(document)
                    if not parsed.nodes:
                        raise MikaSubError("source contains no exportable configs")
                    return item, parsed, ""
                except (ValueError, OSError, asyncio.TimeoutError) as exc:
                    return item, None, str(exc)[:120]

        results = await asyncio.gather(*(collect(item) for item in sources))
        for item, parsed, error in results:
            await self.source_stats(int(item["id"]), parsed, error)
        if any(error for _, _, error in results):
            raise MikaSubError("at least one source failed; last published collection retained")
        totals = {"raw": 0, "parsed": 0, "duplicates": 0, "unsupported": 0, "errors": 0}
        unique: dict[str, Node] = {}
        for _, parsed, _ in results:
            assert parsed is not None
            for name in totals:
                totals[name] += getattr(parsed, name)
            for node in parsed.nodes:
                unique.setdefault(node.identity, node)
        if not unique or len(unique) > 5000:
            raise MikaSubError("published collection must contain 1–5,000 distinct configs")
        totals["duplicates"] += sum(len(parsed.nodes) for _, parsed, _ in results if parsed) - len(unique)
        nodes = list(unique.values())
        ids = await self._ids(nodes)
        nodes.sort(key=lambda node: ids[node.identity])
        items = []
        for node in nodes:
            name = public_name(ids[node.identity])
            items.append({"name": name, "protocol": node.protocol,
                          "uri": rename(node, name)})
        raw = "\n".join(item["uri"] for item in items) + "\n"
        raw_bytes = raw.encode("utf-8")
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        if len(raw_bytes) > 8_000_000 or len(b64) > 11_000_000:
            raise MikaSubError("collection exceeds bounded Worker snapshot size")
        stats = {**totals, "exported": len(items), "source_count": len(sources)}
        start = await self.api("POST", "/api/v1/publish/start", data={
            "total": len(items), "parts": (len(items) + 99) // 100,
            "raw_bytes": len(raw_bytes), "b64_bytes": len(b64), "stats": stats})
        version = str(start.get("version") or "")
        if len(version) != 24:
            raise MikaSubError("Worker did not allocate a snapshot version")
        for offset in range(0, len(items), 100):
            await self.api("POST", f"/api/v1/publish/{version}/items/{offset // 100}",
                           data=items[offset:offset + 100])
        await self.api("POST", f"/api/v1/publish/{version}/raw", text=raw, timeout=45)
        await self.api("POST", f"/api/v1/publish/{version}/b64", text=b64, timeout=45)
        committed = await self.api("POST", f"/api/v1/publish/{version}/commit", data={})
        if committed.get("version") != version or committed.get("total") != len(items):
            raise MikaSubError("Worker publication was not confirmed")
        return BuildResult(version, len(items), totals["raw"], totals["parsed"],
                           totals["duplicates"], totals["unsupported"], totals["errors"])

    async def _rebuild_local(self) -> BuildResult:
        sources = [item for item in await self.sources() if item["enabled"]]
        if not sources:
            raise MikaSubError("add and enable at least one source first")
        gate = asyncio.Semaphore(3)
        async def collect(item: dict) -> Parsed:
            async with gate:
                try:
                    document = await fetch_subscription(str(item["url"]))
                    parsed = parse_subscription(document)
                    if not parsed.nodes:
                        raise MikaSubError("source has no exportable share URIs")
                    await self.source_stats(int(item["id"]), parsed)
                    return parsed
                except (ValueError, OSError, asyncio.TimeoutError) as exc:
                    await self.source_stats(int(item["id"]), None, str(exc)[:120])
                    raise MikaSubError("source fetch failed; previous snapshot retained") from exc
        parsed_sources = await asyncio.gather(*(collect(item) for item in sources))
        unique: dict[str, Node] = {}
        totals = {"raw": 0, "parsed": 0, "duplicates": 0, "unsupported": 0, "errors": 0}
        for parsed in parsed_sources:
            for key in totals:
                totals[key] += getattr(parsed, key)
            for node in parsed.nodes:
                unique.setdefault(node.identity, node)
        if not 1 <= len(unique) <= 5000:
            raise MikaSubError("collection must have 1–5,000 distinct configs")
        totals["duplicates"] += sum(len(p.nodes) for p in parsed_sources) - len(unique)
        node_ids = await self._ids(list(unique.values()))
        items = [{"name": public_name(node_ids[node.identity]),
                  "protocol": node.protocol,
                  "uri": rename(node, public_name(node_ids[node.identity]))}
                 for node in sorted(unique.values(), key=lambda node: node_ids[node.identity])]
        if len(json.dumps(items).encode()) > 8_000_000:
            raise MikaSubError("collection exceeds the 8 MB snapshot limit")
        version = secrets.token_hex(12)
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.db.execute("INSERT INTO mikasub_snapshots VALUES (?,?,?,?)",
            (version, created, json.dumps(items), json.dumps({**totals,
                "exported": len(items), "source_count": len(sources)})))
        return BuildResult(version, len(items), *(totals[key] for key in
            ("raw", "parsed", "duplicates", "unsupported", "errors")))

    async def periodic(self) -> None:
        try:
            while True:
                await asyncio.sleep(6 * 3600)
                if not self.configured and not await self.sources():
                    continue
                try:
                    await self.rebuild()
                except (MikaSubError, ValueError, OSError):
                    pass  # Status is visible in Owner Control; last-known-good is retained.
                try:
                    await self.cleanup()
                except (MikaSubError, ValueError, OSError):
                    pass
        except asyncio.CancelledError:
            raise

    async def cleanup(self) -> int:
        """Delete only expired, non-current KV versions in bounded Worker calls."""
        if not self.configured:
            rows = self.db.execute("SELECT version FROM mikasub_snapshots "
                "ORDER BY rowid DESC").fetchall()
            for row in rows[2:]:
                self.db.execute("DELETE FROM mikasub_snapshots WHERE version=?", (row[0],))
            return max(0, len(rows) - 2)
        result = await self.api("GET", "/api/v1/maintenance/versions")
        versions = result.get("versions")
        if not isinstance(versions, list):
            raise MikaSubError("Worker maintenance response is invalid")
        removed = 0
        for item in versions[:2]:
            version = str(item.get("version") or "")
            parts = int(item.get("parts") or 0)
            if len(version) != 24 or not 0 <= parts <= 50:
                continue
            for offset in range(0, parts + 3, 10):
                answer = await self.api("POST", "/api/v1/maintenance/prune",
                                        data={"version": version, "offset": offset})
                if answer.get("done"):
                    removed += 1
                    break
        return removed

    async def close(self) -> None:
        if self.background:
            self.background.cancel()
            await asyncio.gather(self.background, return_exceptions=True)
            self.background = None
