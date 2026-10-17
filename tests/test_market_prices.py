"""Iran market semantics, exact triggers, and bounded provider access."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from mika_nexus.bot import App
from mika_nexus.config import Settings
from mika_nexus.market_prices import MarketPrices, parse_market_response
from mika_nexus.store import Store
from mika_nexus.utilities import Utilities


def row(symbol: str, price: str, *, source: str = "GHEYMAT",
        quote: str = "IRT", current: bool = True) -> dict:
    return {"symbol": f"{symbol}-{quote}", "price_source": {"symbol": source},
            "base_currency": {"symbol": symbol}, "quote_currency": {"symbol": quote},
            "sell_price": price, "buy_price": price, "is_up_to_date": current}


def test_market_response_rejects_wrong_units_sources_and_bad_values():
    fetched = datetime(2026, 10, 17, tzinfo=timezone.utc)
    snapshot = parse_market_response({"data": [
        row("USD", "267900"), row("SEKE", "269920000"),
        row("EUR", "123", quote="IRR"), row("AED", "100", source="TGJU"),
        row("EUR", "NaN"), row("AED", "123", current=False),
    ]}, fetched)
    assert snapshot.prices == {"USD": (Decimal("267900"), Decimal("267900")),
                               "SEKE": (Decimal("269920000"), Decimal("269920000"))}
    assert snapshot.fetched_at == fetched
    with pytest.raises(ValueError):
        parse_market_response({"data": [row("USD", "0")]}, fetched)
    with pytest.raises(ValueError):
        parse_market_response({"data": "bad"}, fetched)
    ranged = row("USD", "265267")
    ranged["buy_price"] = "265850"
    assert parse_market_response({"data": [ranged]}, fetched).prices["USD"] == (
        Decimal("265267"), Decimal("265850"))


@pytest.mark.asyncio
async def test_market_cache_coalesces_requests_and_shows_provenance(monkeypatch):
    calls = 0
    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.params["quote_currency"] == "IRT"
        return httpx.Response(200, json={"data": [row("USD", "265267"),
            row("EUR", "297984"), row("AED", "72461"), row("SEKE", "268638571")]})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    market = MarketPrices()
    reports = await asyncio.gather(*[market.report(("USD",)) for _ in range(8)])
    assert calls == 1
    assert all("265,267 Toman" in report and "Underlying trade times" in report
               for report in reports)
    assert "Emami gold coin: 268,638,571 Toman" in await market.report(("SEKE",))
    assert calls == 1


@pytest.mark.asyncio
async def test_market_errors_do_not_turn_into_fake_prices(monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs))
    with pytest.raises(httpx.HTTPStatusError):
        await MarketPrices().report(("USD",))


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
    assert [call.args[2] for call in command.await_args_list] == ["USD IRT", "SEKE IRT"]
    message = await send("ارز")
    report.assert_awaited_once_with(("USD", "EUR", "AED"))
    message.reply.assert_awaited_once_with("Market price in Toman")
    await send("دلار؟")
    assert command.await_count == 2


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
    async def send(actor: int, chat: int) -> SimpleNamespace:
        message = SimpleNamespace(from_user=SimpleNamespace(id=actor),
            chat=SimpleNamespace(id=chat, type="supergroup"), text="ارز",
            new_chat_members=None, reply=AsyncMock())
        await app.message(message)
        return message
    assert (await send(2, -100)).reply.await_count == 0
    assert (await send(1, -101)).reply.await_count == 0
    assert (await send(1, -100)).reply.await_count == 0
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
    with pytest.raises(ValueError, match="Official IRR"):
        await utility._command(message, "/rate", "USD IRR")
