"""Rate-limited OpenStreetMap fallback for non-Latin city names."""

import asyncio
import time
from typing import Any

import httpx

_lock = asyncio.Lock()
_last_request = 0.0
_cache: dict[str, tuple[list[dict[str, Any]], float]] = {}


async def nominatim_places(query: str) -> list[dict[str, Any]]:
    global _last_request
    if not 1 <= len(query) <= 80:
        raise ValueError("Enter a city name of at most 80 characters")
    key = query.casefold().strip()
    cached = _cache.get(key)
    if cached and time.monotonic() < cached[1]:
        return cached[0]
    async with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() < cached[1]:
            return cached[0]
        pause = 1.1 - (time.monotonic() - _last_request)
        if pause > 0:
            await asyncio.sleep(pause)
        _last_request = time.monotonic()
        async with httpx.AsyncClient(timeout=9, follow_redirects=False,
                headers={"User-Agent": "Mikachan/2.0 (https://github.com/Dneeel/Mikachan)",
                         "Accept-Language": "en"}) as client:
            response = await client.get("https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "jsonv2", "limit": 3,
                        "addressdetails": 1, "featuretype": "city"})
            response.raise_for_status()
            if len(response.content) > 100_000:
                raise ValueError("Location response is too large")
            data = response.json()
        if not isinstance(data, list):
            raise ValueError("Location service returned an invalid response")
        places: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("address"), dict):
                continue
            address = item["address"]
            try:
                latitude, longitude = float(item["lat"]), float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                continue
            places.append({"name": str(item.get("name") or query)[:80],
                           "country": str(address.get("country") or "")[:80],
                           "country_code": str(address.get("country_code") or "").upper(),
                           "admin1": str(address.get("state") or "")[:80],
                           "latitude": latitude, "longitude": longitude,
                           "population": 1_000_000 if not places else 0})
        if len(_cache) >= 128:
            _cache.clear()
        _cache[key] = places, time.monotonic() + 86400
        return places
