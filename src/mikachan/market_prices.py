"""Iranian market snapshots from a documented, bounded public API."""

import asyncio
import sqlite3
import statistics
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import time

import httpx


MARKET_URL = "https://backend.gheymat.online/api/prices"
WALLEX_URL = "https://api.wallex.ir/v1"
NAVASAN_URL = "https://api.navasan.tech/latest/"
MARKET_TTL_SECONDS = 600
WALLEX_TTL_SECONDS = 30
MARKET_DAILY_BUDGET = 200  # Leave room under the provider's published 250/day free limit.
ASSETS = {
    "USD": "US dollar cash",
    "EUR": "Euro cash",
    "AED": "UAE dirham cash",
    "SEKE": "Emami gold coin",
    "GOLD18": "18K gold per gram",
    "USDT": "Tether (USDT)",
    "BTC": "Bitcoin (BTC)",
    "JPY": "Japanese yen cash",
    "TRY": "Turkish lira cash",
    "OMR": "Omani rial cash",
    "IQD": "Iraqi dinar cash",
    "GBP": "British pound cash",
    "SAR": "Saudi riyal cash",
    "INR": "Indian rupee cash",
    "ETH": "Ethereum (ETH)",
    "TON": "Gram (formerly Toncoin)",
}
CRYPTO_ASSETS = frozenset({"USDT", "BTC", "ETH", "TON"})
CASH_SOURCES = ("TGJU", "NAVASAN", "BONBAST", "ALAN_CHAND")
CRYPTO_SOURCES = ("NOBITEX", "BITPIN", "TETHER_LAND", "TABDEAL")
WALLEX_PAIRS = {"USDT": "USDTTMN", "BTC": "BTCTMN"}
NAVASAN_ITEMS = {"USD": ("usd_buy", "usd_sell"), "EUR": ("eur",),
                 "AED": ("aed_sell",), "SEKE": ("sekkeh",), "GOLD18": ("18ayar",)}
ASSET_ALIASES = {
    "دلار": "USD", "usd": "USD", "یورو": "EUR", "eur": "EUR",
    "درهم": "AED", "aed": "AED", "سکه": "SEKE", "طلا": "GOLD18",
    "تتر": "USDT", "usdt": "USDT", "بیت کوین": "BTC", "بیت‌کوین": "BTC",
    "btc": "BTC", "اتریوم": "ETH", "eth": "ETH", "ین": "JPY", "jpy": "JPY",
    "لیر": "TRY", "try": "TRY", "ریال عمان": "OMR", "omr": "OMR",
    "دینار عراق": "IQD", "دینار": "IQD", "iqd": "IQD", "پوند": "GBP",
    "gbp": "GBP", "ریال سعودی": "SAR", "sar": "SAR", "روپیه": "INR",
    "inr": "INR",
    "ton": "TON", "تون": "TON", "gram": "TON", "گرام": "TON",
}


def parse_asset_query(text: str) -> tuple[str, Decimal] | None:
    """Recognize only an explicit asset name, optionally prefixed with a bounded amount."""
    digits = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    normalized = " ".join(text.translate(digits).casefold().split())
    match = re.fullmatch(r"(?:(\d{1,12}(?:[.,]\d{1,6})?)\s+)?(.{1,32})", normalized)
    if not match:
        return None
    asset = ASSET_ALIASES.get(match[2])
    if not asset:
        return None
    try:
        amount = Decimal(match[1].replace(",", "")) if match[1] else Decimal(1)
    except InvalidOperation:
        return None
    return (asset, amount) if 0 < amount <= Decimal("1000000000") else None


@dataclass(frozen=True)
class MarketQuote:
    source: str
    low: Decimal
    high: Decimal
    observed_at: datetime | None = None
    last_trade: Decimal | None = None

    @property
    def midpoint(self) -> Decimal:
        return (self.low + self.high) / 2


@dataclass(frozen=True)
class MarketSnapshot:
    quotes: dict[str, tuple[MarketQuote, ...]]
    fetched_at: datetime


def _price(value: object) -> Decimal:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid provider price") from exc
    if not price.is_finite() or not 0 < price < Decimal("1000000000000"):
        raise ValueError("Invalid provider price")
    return price


def parse_market_response(payload: object, fetched_at: datetime) -> MarketSnapshot:
    """Validate one or more per-asset rows relayed by the documented Gheymat API."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Iran market provider returned an invalid response")
    found: dict[str, dict[str, MarketQuote]] = {}
    for row in payload["data"]:
        if not isinstance(row, dict):
            continue
        source = row.get("price_source")
        base = row.get("base_currency")
        quote = row.get("quote_currency")
        if not all(isinstance(item, dict) for item in (source, base, quote)):
            continue
        assert isinstance(source, dict) and isinstance(base, dict) and isinstance(quote, dict)
        symbol = base.get("symbol")
        name = source.get("symbol")
        if symbol not in ASSETS or not isinstance(name, str):
            continue
        allowed = CRYPTO_SOURCES if symbol in CRYPTO_ASSETS else CASH_SOURCES
        if name not in allowed or quote.get("symbol") != "IRT":
            continue
        if row.get("symbol") != f"{symbol}-IRT" or row.get("is_up_to_date") is not True:
            continue
        try:
            sell, buy = _price(row["sell_price"]), _price(row["buy_price"])
        except (KeyError, ValueError):
            continue
        low, high = min(sell, buy), max(sell, buy)
        if high / low > Decimal("1.2"):
            continue
        found.setdefault(symbol, {})[name] = MarketQuote(name, low, high)
    quotes = {symbol: tuple(rows[name] for name in
              (CRYPTO_SOURCES if symbol in CRYPTO_ASSETS else CASH_SOURCES) if name in rows)
              for symbol, rows in found.items()}
    if not quotes:
        raise ValueError("No valid Toman source quotes are available")
    return MarketSnapshot(quotes, fetched_at)


def parse_wallex_response(symbol: str, depth: object, trades: object,
                          fetched_at: datetime) -> MarketQuote:
    """Use a direct TMN order book and its separately timed latest trade."""
    pair = WALLEX_PAIRS[symbol]
    if not isinstance(depth, dict) or depth.get("success") is not True:
        raise ValueError("Wallex order book is unavailable")
    if not isinstance(trades, dict) or trades.get("success") is not True:
        raise ValueError("Wallex trades are unavailable")
    book = depth.get("result")
    result = trades.get("result")
    if not isinstance(book, dict) or not isinstance(result, dict):
        raise ValueError("Invalid Wallex response")
    bid_rows, ask_rows, trade_rows = book.get("bid"), book.get("ask"), result.get("latestTrades")
    if not all(isinstance(rows, list) and rows for rows in (bid_rows, ask_rows, trade_rows)):
        raise ValueError("Empty Wallex market response")
    assert isinstance(bid_rows, list) and isinstance(ask_rows, list)
    assert isinstance(trade_rows, list)
    if not all(isinstance(rows[0], dict) for rows in (bid_rows, ask_rows, trade_rows)):
        raise ValueError("Invalid Wallex market rows")
    try:
        bid = _price(bid_rows[0]["price"])
        ask = _price(ask_rows[0]["price"])
        _price(bid_rows[0]["quantity"])
        _price(ask_rows[0]["quantity"])
        trade = trade_rows[0]
        _price(trade["quantity"])
    except KeyError as exc:
        raise ValueError("Incomplete Wallex market response") from exc
    if trade.get("symbol") != pair or bid > ask or ask / bid > Decimal("1.2"):
        raise ValueError("Inconsistent Wallex market response")
    try:
        last_price = _price(trade["price"])
    except KeyError as exc:
        raise ValueError("Incomplete Wallex market response") from exc
    try:
        observed_at = datetime.fromisoformat(str(trade["timestamp"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise ValueError("Invalid Wallex trade time") from exc
    if (observed_at.tzinfo is None or observed_at > fetched_at + timedelta(minutes=2)
            or observed_at < fetched_at - timedelta(days=1)):
        raise ValueError("Wallex trade time is outside the accepted window")
    return MarketQuote("Wallex direct", bid, ask, observed_at, last_price)


def parse_navasan_response(payload: object, fetched_at: datetime) -> dict[str, MarketQuote]:
    """Read provider item timestamps; the URL key is never included in errors."""
    if not isinstance(payload, dict):
        raise ValueError("Navasan returned an invalid response")
    result: dict[str, MarketQuote] = {}
    for symbol, names in NAVASAN_ITEMS.items():
        observations: list[tuple[Decimal, datetime]] = []
        for name in names:
            item = payload.get(name)
            if not isinstance(item, dict):
                continue
            try:
                price = _price(item["value"])
                timestamp = int(item["timestamp"])
                observed = datetime.fromtimestamp(timestamp, timezone.utc)
            except (KeyError, ValueError, TypeError, OverflowError, OSError):
                continue
            if fetched_at - timedelta(days=7) <= observed <= fetched_at + timedelta(minutes=2):
                observations.append((price, observed))
        if observations:
            values = [price for price, _ in observations]
            low, high = min(values), max(values)
            if high / low <= Decimal("1.2"):
                result[symbol] = MarketQuote("Navasan direct", low, high,
                                             min(when for _, when in observations))
    if not result:
        raise ValueError("Navasan returned no valid timestamped Toman quotes")
    return result


def _number(value: Decimal) -> str:
    return f"{value.quantize(Decimal('1'), rounding=ROUND_HALF_UP):,}"


def _range(quote: MarketQuote) -> str:
    low, high = _number(quote.low), _number(quote.high)
    return low if quote.low == quote.high else f"{low}–{high}"


class MarketPrices:
    def __init__(self, db: sqlite3.Connection | None = None, navasan_key: str = "",
                 navasan_refresh_seconds: int = 28_800) -> None:
        self.db = db if db is not None else sqlite3.connect(":memory:", isolation_level=None)
        self.db.execute("CREATE TABLE IF NOT EXISTS market_api_usage ("
                        "day TEXT PRIMARY KEY, calls INTEGER NOT NULL)")
        self._snapshots: dict[str, tuple[MarketSnapshot, float]] = {}
        self._wallex: dict[str, tuple[MarketQuote, datetime, float]] = {}
        self._market_retry_after: dict[str, float] = {}
        self._wallex_retry_after: dict[str, float] = {}
        self._navasan_key = navasan_key
        self._navasan_refresh_seconds = navasan_refresh_seconds
        self._navasan: tuple[dict[str, MarketQuote], float] | None = None
        self._navasan_retry_after = 0.0
        self._ton: tuple[MarketQuote, float] | None = None
        self._navasan_lock = asyncio.Lock()
        self._locks = {symbol: asyncio.Lock() for symbol in ASSETS}
        self._wallex_locks = {symbol: asyncio.Lock() for symbol in WALLEX_PAIRS}

    def _claim_call(self) -> None:
        day = datetime.now(timezone.utc).date().isoformat()
        self.db.execute("INSERT OR IGNORE INTO market_api_usage VALUES (?,0)", (day,))
        claim = self.db.execute("UPDATE market_api_usage SET calls=calls+1 "
                                "WHERE day=? AND calls<?", (day, MARKET_DAILY_BUDGET))
        if claim.rowcount != 1:
            raise ValueError("Iran market API daily budget is exhausted; try again tomorrow")

    async def snapshot(self, symbol: str) -> tuple[MarketSnapshot, bool]:
        if symbol not in ASSETS:
            raise ValueError("Unsupported Iranian market asset")
        cached = self._snapshots.get(symbol)
        if cached and time.monotonic() < cached[1]:
            return cached[0], False
        if time.monotonic() < self._market_retry_after.get(symbol, 0):
            if cached and datetime.now(timezone.utc) - cached[0].fetched_at < timedelta(hours=1):
                return cached[0], True
            raise ValueError("Iran market provider is temporarily unavailable")
        async with self._locks[symbol]:
            cached = self._snapshots.get(symbol)
            if cached and time.monotonic() < cached[1]:
                return cached[0], False
            if time.monotonic() < self._market_retry_after.get(symbol, 0):
                if cached and datetime.now(timezone.utc) - cached[0].fetched_at < timedelta(hours=1):
                    return cached[0], True
                raise ValueError("Iran market provider is temporarily unavailable")
            try:
                self._claim_call()
                async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                    response = await client.get(MARKET_URL, params={
                        "base_currency": symbol, "quote_currency": "IRT"})
                    response.raise_for_status()
                    if len(response.content) > 1_000_000:
                        raise ValueError("Iran market response is too large")
                    snapshot = parse_market_response(response.json(), datetime.now(timezone.utc))
                if symbol not in snapshot.quotes:
                    raise ValueError(f"No valid {symbol}/IRT source quotes are available")
            except (ValueError, httpx.HTTPError):
                self._market_retry_after[symbol] = time.monotonic() + 30
                if cached and datetime.now(timezone.utc) - cached[0].fetched_at < timedelta(hours=1):
                    return cached[0], True
                raise
            self._snapshots[symbol] = snapshot, time.monotonic() + MARKET_TTL_SECONDS
            return snapshot, False

    async def wallex(self, symbol: str) -> tuple[MarketQuote, datetime] | None:
        if symbol not in WALLEX_PAIRS:
            return None
        cached = self._wallex.get(symbol)
        if cached and time.monotonic() < cached[2]:
            return cached[0], cached[1]
        if time.monotonic() < self._wallex_retry_after.get(symbol, 0):
            return None
        async with self._wallex_locks[symbol]:
            cached = self._wallex.get(symbol)
            if cached and time.monotonic() < cached[2]:
                return cached[0], cached[1]
            if time.monotonic() < self._wallex_retry_after.get(symbol, 0):
                return None
            pair = WALLEX_PAIRS[symbol]
            try:
                async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                    depth, trades = await asyncio.gather(
                        client.get(f"{WALLEX_URL}/depth", params={"symbol": pair}),
                        client.get(f"{WALLEX_URL}/trades", params={"symbol": pair}),
                    )
                    depth.raise_for_status()
                    trades.raise_for_status()
                    if max(len(depth.content), len(trades.content)) > 512_000:
                        raise ValueError("Wallex response is too large")
                    fetched_at = datetime.now(timezone.utc)
                    quote = parse_wallex_response(symbol, depth.json(), trades.json(), fetched_at)
            except (ValueError, httpx.HTTPError, KeyError, TypeError):
                self._wallex_retry_after[symbol] = time.monotonic() + 10
                return None
            self._wallex[symbol] = quote, fetched_at, time.monotonic() + WALLEX_TTL_SECONDS
            return quote, fetched_at

    async def navasan(self, symbol: str) -> MarketQuote | None:
        try:
            row = self.db.execute("SELECT api_key FROM content_api_credentials "
                                  "WHERE provider='navasan'").fetchone()
        except sqlite3.OperationalError:
            row = None  # Standalone market tests and installations predating ContentUI.
        key = str(row[0]) if row else self._navasan_key
        if not key or symbol not in NAVASAN_ITEMS:
            return None
        if getattr(self, "_active_navasan_key", None) != key:
            self._active_navasan_key = key
            self._navasan = None
            self._navasan_retry_after = 0.0
        cached = self._navasan
        if cached and time.monotonic() < cached[1]:
            return cached[0].get(symbol)
        if time.monotonic() < self._navasan_retry_after:
            return None
        async with self._navasan_lock:
            cached = self._navasan
            if cached and time.monotonic() < cached[1]:
                return cached[0].get(symbol)
            if time.monotonic() < self._navasan_retry_after:
                return None
            try:
                async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                    response = await client.get(NAVASAN_URL, params={"api_key": key})
                    if response.status_code != 200:
                        raise ValueError(f"Navasan returned HTTP {response.status_code}")
                    if len(response.content) > 512_000:
                        raise ValueError("Navasan response is too large")
                    quotes = parse_navasan_response(response.json(), datetime.now(timezone.utc))
            except (ValueError, httpx.HTTPError, TypeError):
                self._navasan_retry_after = time.monotonic() + 60
                return None
            self._navasan = quotes, time.monotonic() + self._navasan_refresh_seconds
            return quotes.get(symbol)

    async def _section(self, symbol: str) -> list[str]:
        quotes, snapshot, stale, direct, navasan, navasan_consistent, navasan_recent = \
            await self._collect(symbol)
        midpoints = [quote.midpoint for quote in quotes]
        median = statistics.median(midpoints)
        spread = (max(midpoints) - min(midpoints)) / median * 100 if median else Decimal(0)
        if len(quotes) == 1:
            lines = [f"{ASSETS[symbol]}: {_range(quotes[0])} Toman (one source only)"]
        elif spread > 5:
            lines = [f"{ASSETS[symbol]}: no reliable consensus "
                     f"({len(quotes)} sources; spread {spread:.1f}%)"]
        else:
            lines = [f"{ASSETS[symbol]}: ~{_number(median)} Toman "
                     f"(median of {len(quotes)} sources; spread {spread:.1f}%)"]
        for quote in quotes:
            label = (quote.source if quote.source.endswith(" direct") else
                     f"{quote.source} via Gheymat")
            lines.append(f"  {label}: {_range(quote)}")
        if spread > 5:
            lines.append("  Providers disagree substantially; compare source quotes directly.")
        if direct:
            observed = direct[0].observed_at
            assert observed is not None and direct[0].last_trade is not None
            lines.append(f"  Wallex last trade: {_number(direct[0].last_trade)} at "
                         f"{observed:%Y-%m-%d %H:%M:%S} UTC")
        if navasan and navasan.observed_at:
            if not navasan_consistent:
                age_note = " (inconsistent with other sources; excluded from median)"
            else:
                age_note = "" if navasan_recent else " (over 2h old; excluded from median)"
            lines.append(f"  Navasan observed {navasan.observed_at:%Y-%m-%d %H:%M:%S} UTC"
                         f"{age_note}")
        if stale:
            lines.append("  Relay provider unavailable; showing a cached snapshot.")
        if snapshot is not None:
            lines.append(f"  Relay fetched {snapshot.fetched_at:%Y-%m-%d %H:%M} UTC; "
                         "underlying source trade times unknown.")
        elif direct or navasan_recent:
            lines.append("  Relay provider unavailable; only a direct quote is shown.")
        elif symbol == "TON" and quotes[0].observed_at:
            lines.append(f"  Bitpin observed {quotes[0].observed_at:%Y-%m-%d %H:%M:%S} UTC.")
        return lines

    async def _collect(self, symbol: str) -> tuple[list[MarketQuote], MarketSnapshot | None,
                                                   bool, tuple[MarketQuote, datetime] | None,
                                                   MarketQuote | None, bool, bool]:
        if symbol == "TON":
            quote = await self.bitpin_ton()
            return [quote], None, False, None, None, True, False
        snapshot_result, direct_result, navasan_result = await asyncio.gather(
            self.snapshot(symbol), self.wallex(symbol), self.navasan(symbol),
            return_exceptions=True)
        if isinstance(snapshot_result, asyncio.CancelledError):
            raise snapshot_result
        if isinstance(direct_result, asyncio.CancelledError):
            raise direct_result
        if isinstance(navasan_result, asyncio.CancelledError):
            raise navasan_result
        snapshot = None
        stale = False
        relay_quotes: list[MarketQuote] = []
        if not isinstance(snapshot_result, BaseException):
            snapshot, stale = snapshot_result
            relay_quotes = list(snapshot.quotes[symbol])
        direct = direct_result if not isinstance(direct_result, BaseException) else None
        navasan = navasan_result if not isinstance(navasan_result, BaseException) else None
        other_relay = [quote for quote in relay_quotes if quote.source != "NAVASAN"]
        navasan_consistent = True
        if navasan is not None and other_relay:
            reference = statistics.median([quote.midpoint for quote in other_relay])
            navasan_consistent = (Decimal("0.9") <= navasan.midpoint / reference <= Decimal("1.1"))
        if navasan is not None:
            relay_quotes = other_relay
        navasan_recent = bool(navasan and navasan.observed_at and
                              datetime.now(timezone.utc) - navasan.observed_at < timedelta(hours=2)
                              and navasan_consistent)
        quotes = ([direct[0]] if direct else []) + ([navasan] if navasan_recent and navasan else []) + relay_quotes
        if not quotes:
            if isinstance(snapshot_result, BaseException):
                raise snapshot_result
            raise ValueError(f"No valid {symbol}/IRT source quotes are available")
        return quotes, snapshot, stale, direct, navasan, navasan_consistent, navasan_recent

    async def bitpin_ton(self) -> MarketQuote:
        """Bitpin lists the renamed Toncoin market as GRAM_IRT."""
        if self._ton and time.monotonic() < self._ton[1]:
            return self._ton[0]
        async with self._locks["TON"]:
            if self._ton and time.monotonic() < self._ton[1]:
                return self._ton[0]
            async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
                response = await client.get("https://api.bitpin.ir/v1/mkt/markets/")
                response.raise_for_status()
                if len(response.content) > 4_000_000:
                    raise ValueError("Bitpin market catalog is too large")
                payload = response.json()
            rows = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise ValueError("Bitpin market catalog is unavailable")
            market = next((row for row in rows if isinstance(row, dict) and
                           row.get("code") == "GRAM_IRT" and row.get("tradable") is True and
                           row.get("suspended") is False), None)
            if market is None or not isinstance(market.get("price_info"), dict):
                raise ValueError("Gram/Toman market is unavailable")
            info = market["price_info"]
            try:
                price = _price(info["price"])
                observed = datetime.fromtimestamp(float(info["created_at"]), timezone.utc)
            except (KeyError, ValueError, TypeError, OSError, OverflowError) as exc:
                raise ValueError("Bitpin returned an invalid Gram quote") from exc
            now = datetime.now(timezone.utc)
            if not now - timedelta(minutes=15) <= observed <= now + timedelta(minutes=2):
                raise ValueError("Gram/Toman quote is stale")
            quote = MarketQuote("Bitpin direct", price, price, observed)
            self._ton = quote, time.monotonic() + 120
            return quote

    async def amount_card(self, symbol: str, amount: Decimal) -> str:
        if symbol not in ASSETS or not amount.is_finite() or not 0 < amount <= Decimal("1000000000"):
            raise ValueError("Unsupported asset or amount")
        quotes, snapshot, stale, direct, navasan, _, _ = await self._collect(symbol)
        midpoints = [quote.midpoint for quote in quotes]
        median = statistics.median(midpoints)
        spread = (max(midpoints) - min(midpoints)) / median * 100
        if spread > 5:
            raise ValueError(f"Sources disagree on {symbol} ({spread:.1f}% spread). Try again later.")
        value = _number(median * amount)
        basis = "one source" if len(quotes) == 1 else f"median of {len(quotes)} sources"
        source_time = (snapshot.fetched_at if snapshot else
                       direct[1] if direct else navasan.observed_at if navasan else
                       quotes[0].observed_at or datetime.now(timezone.utc))
        note = "Cached snapshot · " if stale else ""
        return (f"💱 <b>{_number(amount)} {ASSETS[symbol]}</b>\n"
                f"💰 <b>~{value} Toman</b>\n"
                f"1 {symbol} ≈ {_number(median)} Toman · {basis}\n"
                f"{note}Fetched {source_time:%Y-%m-%d %H:%M} UTC"
                "\nIndicative quote · source trade times may differ")

    async def report(self, symbols: tuple[str, ...]) -> str:
        if not symbols or any(symbol not in ASSETS for symbol in symbols):
            raise ValueError("Unsupported Iranian market asset")
        results = await asyncio.gather(*(self._section(symbol) for symbol in symbols),
                                       return_exceptions=True)
        lines = ["Iran market · Toman per unit · indicative quotes"]
        for symbol, result in zip(symbols, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                detail = str(result) if isinstance(result, ValueError) else "provider request failed"
                lines.append(f"{ASSETS[symbol]}: unavailable ({detail})")
            else:
                lines.extend(result)
        if all(isinstance(result, BaseException) for result in results):
            raise ValueError("; ".join(lines[1:])[:500])
        lines.append("Relayed ranges are provider quote fields with unverified buy/sell direction.")
        lines.append("Cash FX is distinct from USDT. Quotes are informational, not executable offers.")
        return "\n".join(lines)
