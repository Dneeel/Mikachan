"""Iran market semantics, exact triggers, and bounded provider access."""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from mikachan.bot import App
from mikachan.config import Settings
from mikachan.market_prices import (MarketPrices, parse_market_response,
                                      parse_navasan_response, parse_wallex_response)
from mikachan.store import Store
from mikachan.utilities import Utilities


def row(symbol: str, price: str, *, source: str = "TGJU",
        quote: str = "IRT", current: bool = True) -> dict:
    return {"symbol": f"{symbol}-{quote}", "price_source": {"symbol": source},
            "base_currency": {"symbol": symbol}, "quote_currency": {"symbol": quote},
            "sell_price": price, "buy_price": price, "is_up_to_date": current}


def test_market_response_rejects_wrong_units_sources_and_bad_values():
    fetched = datetime(2026, 10, 17, tzinfo=timezone.utc)
    snapshot = parse_market_response({"data": [
        row("USD", "267900"), row("SEKE", "269920000"),
        row("EUR", "123", quote="IRR"), row("AED", "100", source="GHEYMAT"),
        row("EUR", "NaN"), row("AED", "123", current=False),
    ]}, fetched)
    assert snapshot.quotes["USD"][0].source == "TGJU"
    assert snapshot.quotes["USD"][0].low == Decimal("267900")
    assert snapshot.quotes["SEKE"][0].high == Decimal("269920000")
    assert snapshot.fetched_at == fetched
    with pytest.raises(ValueError):
        parse_market_response({"data": [row("USD", "0")]}, fetched)
    with pytest.raises(ValueError):
        parse_market_response({"data": "bad"}, fetched)
    ranged = row("USD", "265267")
    ranged["buy_price"] = "265850"
    quote = parse_market_response({"data": [ranged]}, fetched).quotes["USD"][0]
    assert (quote.low, quote.high) == (Decimal("265267"), Decimal("265850"))
    assert quote.observed_at is None
    with pytest.raises(ValueError):
        parse_market_response({"data": [row("USD", "267900", source="GHEYMAT")]}, fetched)


def test_wallex_quote_requires_valid_book_pair_and_trade_time():
    now = datetime.now(timezone.utc)
    book = {"success": True, "result": {"bid": [{"price": 263100, "quantity": 1}],
                                         "ask": [{"price": 263200, "quantity": 1}]}}
    trades = {"success": True, "result": {"latestTrades": [{
        "symbol": "USDTTMN", "price": "263150", "quantity": 1, "timestamp":
        (now - timedelta(seconds=20)).isoformat()}]}}
    quote = parse_wallex_response("USDT", book, trades, now)
    assert quote.source == "Wallex direct"
    assert quote.low == Decimal("263100") and quote.high == Decimal("263200")
    assert quote.last_trade == Decimal("263150") and quote.observed_at is not None
    trades["result"]["latestTrades"][0]["symbol"] = "USDIRT"
    with pytest.raises(ValueError, match="Inconsistent"):
        parse_wallex_response("USDT", book, trades, now)
    trades["result"]["latestTrades"][0]["symbol"] = "USDTTMN"
    trades["result"]["latestTrades"][0]["timestamp"] = (
        now - timedelta(days=2)).isoformat()
    with pytest.raises(ValueError, match="time"):
        parse_wallex_response("USDT", book, trades, now)
    trades["result"]["latestTrades"][0].pop("price")
    with pytest.raises(ValueError, match="Incomplete"):
        parse_wallex_response("USDT", book, trades, now)


def test_navasan_direct_timestamps_and_invalid_quotes():
    now = datetime.now(timezone.utc)
    observed = int((now - timedelta(minutes=2)).timestamp())
    old = int((now - timedelta(days=9)).timestamp())
    rows = {"usd_buy": {"value": "264100", "timestamp": observed},
            "usd_sell": {"value": "264500", "timestamp": observed},
            "sekkeh": {"value": "269000000", "timestamp": observed},
            "18ayar": {"value": "0", "timestamp": observed},
            "eur": {"value": "300000", "timestamp": old}}
    quotes = parse_navasan_response(rows, now)
    assert quotes["USD"].source == "Navasan direct"
    assert (quotes["USD"].low, quotes["USD"].high) == (
        Decimal("264100"), Decimal("264500"))
    assert quotes["SEKE"].observed_at is not None
    assert "GOLD18" not in quotes and "EUR" not in quotes
    with pytest.raises(ValueError):
        parse_navasan_response({"usd_sell": {"value": "NaN", "timestamp": observed}}, now)


@pytest.mark.asyncio
async def test_keyed_navasan_once_for_multiple_assets_and_no_secret_in_error(monkeypatch):
    calls = 0
    now = int(datetime.now(timezone.utc).timestamp())
    real_client = httpx.AsyncClient
    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.params["api_key"] == "test-only-secret"
        return httpx.Response(200, json={"usd_sell": {"value": 265000, "timestamp": now},
            "sekkeh": {"value": 269000000, "timestamp": now}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    market = MarketPrices(navasan_key="test-only-secret")
    usd, coin = await asyncio.gather(market.navasan("USD"), market.navasan("SEKE"))
    assert calls == 1 and usd and coin
    assert usd.observed_at and usd.low == Decimal("265000")

    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(fail), **kwargs))
    assert await MarketPrices(navasan_key="test-only-secret").navasan("USD") is None


@pytest.mark.asyncio
async def test_direct_navasan_replaces_relay_copy_and_rejects_unit_mismatch(monkeypatch):
    observed = int(datetime.now(timezone.utc).timestamp())
    real_client = httpx.AsyncClient
    value = "264500"
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.navasan.tech":
            return httpx.Response(200, json={"usd_sell": {"value": value,
                                                            "timestamp": observed}})
        return httpx.Response(200, json={"data": [row("USD", "265000"),
            row("USD", "265100", source="NAVASAN"),
            row("USD", "264000", source="BONBAST")]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    report = await MarketPrices(navasan_key="test-only-secret").report(("USD",))
    assert "Navasan direct: 264,500" in report
    assert "NAVASAN via Gheymat: 265,100" not in report
    assert "Navasan observed" in report
    value = "2645000"  # Detect Rial/Toman scale mistakes before a false consensus.
    report = await MarketPrices(navasan_key="test-only-secret").report(("USD",))
    assert "inconsistent with other sources; excluded" in report
    assert "Navasan direct: 2,645,000" not in report


@pytest.mark.asyncio
async def test_market_cache_coalesces_requests_and_shows_provenance(monkeypatch):
    calls = 0
    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.params["quote_currency"] == "IRT"
        symbol = request.url.params["base_currency"]
        return httpx.Response(200, json={"data": [row(symbol, "265267"),
            row(symbol, "265850", source="NAVASAN")]})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    market = MarketPrices()
    reports = await asyncio.gather(*[market.report(("USD",)) for _ in range(8)])
    assert calls == 1
    assert all("median of 2 sources" in report and "source trade times unknown" in report
               for report in reports)
    assert "TGJU via Gheymat: 265,267" in reports[0]
    assert "NAVASAN via Gheymat: 265,850" in reports[0]
    assert "Emami gold coin" in await market.report(("SEKE",))
    assert calls == 2


@pytest.mark.asyncio
async def test_market_errors_do_not_turn_into_fake_prices(monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs))
    with pytest.raises(ValueError, match="unavailable"):
        await MarketPrices().report(("USD",))


@pytest.mark.asyncio
async def test_market_daily_budget_and_labeled_cached_fallback(monkeypatch):
    calls = 0
    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": [row("USD", "267900")]})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    db = sqlite3.connect(":memory:", isolation_level=None)
    market = MarketPrices(db)
    await market.report(("USD",))
    assert calls == 1
    day = datetime.now(timezone.utc).date().isoformat()
    db.execute("UPDATE market_api_usage SET calls=200 WHERE day=?", (day,))
    old, _ = market._snapshots["USD"]
    market._snapshots["USD"] = old, 0.0
    report = await market.report(("USD",))
    assert "cached snapshot" in report and calls == 1
    with pytest.raises(ValueError, match="budget"):
        await market.snapshot("EUR")


@pytest.mark.asyncio
async def test_crypto_report_uses_direct_wallex_and_relay_without_double_count(monkeypatch):
    now = datetime.now(timezone.utc) - timedelta(seconds=10)
    calls: list[str] = []
    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "backend.gheymat.online":
            return httpx.Response(200, json={"data": [
                row("USDT", "263000", source="NOBITEX"),
                row("USDT", "263100", source="BITPIN"),
                row("USDT", "260000", source="WALLEX"),
            ]})
        if request.url.path.endswith("/depth"):
            return httpx.Response(200, json={"success": True, "result": {
                "bid": [{"price": "263050", "quantity": 1}],
                "ask": [{"price": "263150", "quantity": 1}]}})
        return httpx.Response(200, json={"success": True, "result": {"latestTrades": [{
            "symbol": "USDTTMN", "price": "263100", "quantity": 1,
            "timestamp": now.isoformat()}]}})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    market = MarketPrices()
    report = await market.report(("USDT",))
    assert "Wallex direct: 263,050–263,150" in report
    assert "Wallex last trade: 263,100 at" in report
    assert "median of 3 sources" in report
    assert "WALLEX via Gheymat: 260,000" not in report
    assert len(calls) == 3
    await market.report(("USDT",))
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_direct_crypto_survives_relay_failure_and_outliers_are_not_consensus(monkeypatch):
    observed = datetime.now(timezone.utc).isoformat()
    real_client = httpx.AsyncClient
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "backend.gheymat.online":
            return httpx.Response(503)
        if request.url.path.endswith("/depth"):
            return httpx.Response(200, json={"success": True, "result": {
                "bid": [{"price": 260000, "quantity": 1}],
                "ask": [{"price": 261000, "quantity": 1}]}})
        return httpx.Response(200, json={"success": True, "result": {"latestTrades": [{
            "symbol": "USDTTMN", "price": 260500, "quantity": 1,
            "timestamp": observed}]}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    report = await MarketPrices().report(("USDT",))
    assert "one source only" in report and "Relay provider unavailable" in report

    def divergent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [row("USD", "100000"),
            row("USD", "200000", source="NAVASAN")]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(divergent), **kwargs))
    report = await MarketPrices().report(("USD",))
    assert "no reliable consensus" in report
    assert "~150,000" not in report


@pytest.mark.asyncio
async def test_exact_persian_prices_and_private_access(tmp_path):
    app = object.__new__(App)
    app.settings = Settings(bot_token="123:fake", owner_telegram_id=1,
                            allowed_telegram_ids="1", data_dir=tmp_path)
    app.store = Store(tmp_path / "state.sqlite")
    app.store.seed_access({1})
    app.community = SimpleNamespace(search_reply=AsyncMock(return_value=False))
    report = AsyncMock(return_value="Market price in Toman")
    command = AsyncMock(return_value=True)
    app.utilities = SimpleNamespace(market_prices=SimpleNamespace(report=report),
                                    command=command)
    async def send(value: str, actor: int = 1) -> SimpleNamespace:
        message = SimpleNamespace(from_user=SimpleNamespace(id=actor),
            chat=SimpleNamespace(id=actor, type="private"), text=value,
            new_chat_members=None, reply=AsyncMock())
        await app.message(message)
        return message
    assert (await send("دلار", 2)).reply.await_count == 0
    assert command.await_count == 0
    await send("دلار")
    await send("سکه")
    await send("طلا")
    await send("تتر")
    await send("بیت‌کوین")
    assert [call.args[2] for call in command.await_args_list] == [
        "USD IRT", "SEKE IRT", "GOLD18 IRT", "USDT IRT", "BTC IRT"]
    message = await send("ارز")
    report.assert_awaited_once_with(("USD", "EUR", "AED"))
    message.reply.assert_awaited_once_with("Market price in Toman")
    await send("دلار؟")
    await send("طلا؟")
    await send("قیمت بیت‌کوین")
    assert command.await_count == 5


@pytest.mark.asyncio
async def test_persian_prices_respect_group_allowlist_and_module_setting(tmp_path):
    app = object.__new__(App)
    app.settings = Settings(bot_token="123:fake", owner_telegram_id=1,
                            allowed_telegram_ids="1", data_dir=tmp_path,
                            private_chats_only=False)
    app.store = Store(tmp_path / "state.sqlite")
    app.store.seed_access({1})
    app.store.seed_groups({-100})
    report = AsyncMock(return_value="Market price")
    command = AsyncMock(return_value=True)
    app.utilities = SimpleNamespace(market_prices=SimpleNamespace(report=report), command=command)
    enabled = False
    app.community = SimpleNamespace(store=SimpleNamespace(
        enabled=lambda chat, module: enabled,
        item=lambda *args: None, use=lambda *args: None),
        search_reply=AsyncMock(return_value=False))
    async def send(actor: int, chat: int, value: str = "ارز") -> SimpleNamespace:
        message = SimpleNamespace(from_user=SimpleNamespace(id=actor),
            chat=SimpleNamespace(id=chat, type="supergroup"), text=value,
            new_chat_members=None, reply=AsyncMock())
        await app.message(message)
        return message
    assert (await send(2, -100)).reply.await_count == 0
    assert (await send(1, -101)).reply.await_count == 0
    assert (await send(1, -100)).reply.await_count == 0
    await send(1, -100, "طلا")
    assert command.await_count == 0
    enabled = True
    assert (await send(1, -100)).reply.await_count == 1
    report.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_irt_uses_local_market_instead_of_global_reference(tmp_path):
    db = Store(tmp_path / "state.sqlite").db
    settings = Settings(bot_token="123:fake", owner_telegram_id=1, allowed_telegram_ids="1")
    utility = Utilities(SimpleNamespace(), settings, db)
    utility.market_prices.report = AsyncMock(return_value="Iran market quote")
    utility.api = AsyncMock(side_effect=AssertionError("global fiat API must not be used"))
    message = SimpleNamespace(reply=AsyncMock())
    await utility._command(message, "/rate", "USD IRT")
    message.reply.assert_awaited_once_with("Iran market quote")
    await utility._command(message, "/rate", "GOLD18 IRT")
    await utility._command(message, "/rate", "BTC IRT")
    assert utility.market_prices.report.await_count == 3
    with pytest.raises(ValueError, match="Official IRR"):
        await utility._command(message, "/rate", "USD IRR")


@pytest.mark.asyncio
async def test_utility_provider_errors_do_not_echo_credential_urls(tmp_path):
    db = Store(tmp_path / "state.sqlite").db
    settings = Settings(bot_token="123:fake", owner_telegram_id=1, allowed_telegram_ids="1")
    utility = Utilities(SimpleNamespace(), settings, db)
    request = httpx.Request("GET", "https://example.org/feed?api_key=secret-value")
    response = httpx.Response(401, request=request)
    utility._command = AsyncMock(side_effect=httpx.HTTPStatusError(
        "unauthorized", request=request, response=response))
    message = SimpleNamespace(reply=AsyncMock())
    assert await utility.command(message, "/weather", "Tehran")
    message.reply.assert_awaited_once_with("External provider request failed. Try again later.")
