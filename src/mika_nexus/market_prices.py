"""Iranian market snapshots from a documented, bounded public API."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import time

import httpx


MARKET_URL = "https://backend.gheymat.online/api/prices"
MARKET_TTL_SECONDS = 390  # 222 refreshes/day per continuously running instance.
MARKET_SYMBOLS = {
    "USD": "US dollar (open market)",
    "EUR": "Euro (open market)",
    "AED": "UAE dirham (open market)",
    "SEKE": "Emami gold coin",
}


@dataclass(frozen=True)
class MarketSnapshot:
    prices: dict[str, tuple[Decimal, Decimal]]
    fetched_at: datetime


def parse_market_response(payload: object, fetched_at: datetime) -> MarketSnapshot:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Iran market provider returned an invalid response")
    prices: dict[str, tuple[Decimal, Decimal]] = {}
    for row in payload["data"]:
        if not isinstance(row, dict):
            continue
        source = row.get("price_source")
        if not isinstance(source, dict) or source.get("symbol") != "GHEYMAT":
            continue
        base = row.get("base_currency")
        quote = row.get("quote_currency")
        if not isinstance(base, dict) or not isinstance(quote, dict):
            continue
        symbol = base.get("symbol")
        if symbol not in MARKET_SYMBOLS or quote.get("symbol") != "IRT":
            continue
        if row.get("symbol") != f"{symbol}-IRT" or row.get("is_up_to_date") is not True:
            continue
        try:
            sell = Decimal(str(row["sell_price"]))
            buy = Decimal(str(row["buy_price"]))
        except (KeyError, InvalidOperation, ValueError):
            continue
        if all(value.is_finite() and 0 < value < Decimal("1000000000000")
               for value in (sell, buy)):
            prices[symbol] = (min(sell, buy), max(sell, buy))
    if not prices:
        raise ValueError("No valid Toman prices are available from the Iran market provider")
    return MarketSnapshot(prices, fetched_at)


class MarketPrices:
    def __init__(self) -> None:
        self._snapshot: MarketSnapshot | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def snapshot(self) -> MarketSnapshot:
        if self._snapshot is not None and time.monotonic() < self._expires_at:
            return self._snapshot
        async with self._lock:
            if self._snapshot is not None and time.monotonic() < self._expires_at:
                return self._snapshot
            async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                response = await client.get(MARKET_URL, params={"quote_currency": "IRT"})
                response.raise_for_status()
                if len(response.content) > 2_000_000:
                    raise ValueError("Iran market provider response is too large")
                snapshot = parse_market_response(response.json(), datetime.now(timezone.utc))
            self._snapshot = snapshot
            self._expires_at = time.monotonic() + MARKET_TTL_SECONDS
            return snapshot

    async def report(self, symbols: tuple[str, ...]) -> str:
        snapshot = await self.snapshot()
        missing = set(symbols) - snapshot.prices.keys()
        if missing:
            raise ValueError("No current Toman quote for " + ", ".join(sorted(missing)))
        lines = ["Iran market · Toman per unit"]
        for symbol in symbols:
            low, high = snapshot.prices[symbol]
            value = f"{low:,.0f}" if low == high else f"{low:,.0f}–{high:,.0f}"
            lines.append(f"{MARKET_SYMBOLS[symbol]}: {value} Toman")
        lines.append("Gheymat multi-source estimate · "
                     f"fetched {snapshot.fetched_at:%Y-%m-%d %H:%M} UTC")
        lines.append("Indicative quote range; buying and selling direction is not verified.")
        lines.append("Underlying trade times are not supplied; a closed-market quote may be older.")
        return "\n".join(lines)
