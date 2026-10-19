"""Small, attributed, on-demand window into Ganjoor's public poetry API."""

import html
import time
from typing import Any

import httpx

BASE = "https://api.ganjoor.net/api/ganjoor"


class Ganjoor:
    def __init__(self) -> None:
        self._poets: tuple[list[tuple[int, str]], float] | None = None

    async def _get(self, path: str, params: dict[str, str | int] | None = None) -> Any:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(BASE + path, params=params)
            response.raise_for_status()
            if len(response.content) > 500_000:
                raise ValueError("Poetry provider response is too large")
            return response.json()

    async def poets(self) -> list[tuple[int, str]]:
        if self._poets and time.monotonic() < self._poets[1]:
            return self._poets[0]
        data = await self._get("/poets")
        if not isinstance(data, list):
            raise ValueError("Poetry catalog is unavailable")
        poets = [(row["id"], row["name"][:60]) for row in data
                 if isinstance(row, dict) and isinstance(row.get("id"), int)
                 and 0 < row["id"] < 1_000_000 and isinstance(row.get("name"), str)
                 and row.get("published") is True]
        if not poets:
            raise ValueError("Poetry catalog is empty")
        self._poets = poets, time.monotonic() + 86400
        return poets

    async def random(self, poet_id: int | None = None) -> dict[str, Any]:
        data = await self._get("/poem/random", {"poetId": poet_id} if poet_id else None)
        if not isinstance(data, dict):
            raise ValueError("No poem was returned")
        return data

    async def search(self, term: str) -> dict[str, Any] | None:
        data = await self._get("/poems/search", {"term": term, "PageSize": 5})
        if not isinstance(data, list):
            raise ValueError("Poetry search is unavailable")
        return next((row for row in data if isinstance(row, dict)
                     and term.casefold() in str(row.get("plainText", "")).casefold()), None)


def render_poem(poem: dict[str, Any]) -> str:
    title, body, path = (poem.get(key) for key in ("fullTitle", "plainText", "fullUrl"))
    if not isinstance(title, str) or not isinstance(body, str) or not isinstance(path, str) \
            or not path.startswith("/") or path.startswith("//") or len(path) > 200:
        raise ValueError("Poetry provider returned an incomplete poem")
    excerpt = body.strip()[:3100]
    if len(body.strip()) > 3100:
        excerpt = excerpt.rsplit("\n", 1)[0] + "\n…"
    return f"📜 <b>{html.escape(title[:150])}</b>\n\n{html.escape(excerpt)}"
