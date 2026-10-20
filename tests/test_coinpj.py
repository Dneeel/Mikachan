from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest

from mikachan.coinpj import CoinPJ, coinpj_prompt, parse_coinpj
from mikachan.config import Settings
from mikachan.store import Store
from mikachan.utilities import Utilities


def test_coinpj_quote_is_bound_to_amount_and_asset():
    reply = ("✨ 100 ین ژاپن :\n\n💸 131,900 toman\n"
             "💵 $0.63246527 dollar\n\n🪙 1405/06/10 | 00:35:57")
    assert coinpj_prompt("JPY", Decimal(100)) == "100 ین"
    card = parse_coinpj(reply, "JPY", Decimal(100))
    assert "131,900 Toman" in card and "0.63246527 USD" in card
    assert "1405/06/10 · 00:35:57" in card
    with pytest.raises(ValueError, match="different"):
        parse_coinpj(reply, "JPY", Decimal(1))
    with pytest.raises(ValueError, match="asset identity"):
        parse_coinpj(reply, "USD", Decimal(100))


def test_coinpj_session_requires_telegram_app_credentials():
    with pytest.raises(ValueError, match="Telegram API ID and hash"):
        Settings(bot_token="123:fake", owner_telegram_id=1, allowed_telegram_ids="1",
                 coinpj_session="secret")


def test_userbot_session_also_enables_coinpj_quotes(tmp_path, monkeypatch):
    created = []

    def fake_coinpj(*args):
        created.append(args)
        return object()

    monkeypatch.setattr("mikachan.utilities.CoinPJ", fake_coinpj)
    settings = Settings(bot_token="123:fake", owner_telegram_id=1, allowed_telegram_ids="1",
                        telegram_api_id=12345, telegram_api_hash="hash", userbot_session="session")
    utility = Utilities(SimpleNamespace(), settings, Store(tmp_path / "db.sqlite").db)
    assert created == [(12345, "hash", "session")]
    assert utility.coinpj is not None


@pytest.mark.asyncio
async def test_toman_command_prefers_bound_userbot_quote(tmp_path):
    settings = Settings(bot_token="123:fake", owner_telegram_id=1, allowed_telegram_ids="1")
    utility = Utilities(SimpleNamespace(), settings, Store(tmp_path / "db.sqlite").db)
    utility.coinpj = SimpleNamespace(quote=AsyncMock(return_value="CoinPJ verified quote"))
    utility.market_prices.amount_card = AsyncMock(side_effect=AssertionError("wrong provider"))
    message = SimpleNamespace(reply=AsyncMock())
    assert await utility.command(message, "/rate", "100 JPY IRT") is False
    await utility.asset_quote(message, "100 JPY IRT")
    utility.coinpj.quote.assert_awaited_once_with("JPY", Decimal(100))
    assert message.reply.await_args.args[0] == "CoinPJ verified quote"


@pytest.mark.asyncio
async def test_coinpj_rejects_wrong_sender_and_reply_binding():
    class Conversation:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return None
        async def send_message(self, _prompt):
            return SimpleNamespace(id=100)
        async def get_response(self):
            return response
    client = SimpleNamespace(is_connected=lambda: True,
        is_user_authorized=AsyncMock(return_value=True),
        conversation=lambda *_args, **_kwargs: Conversation())
    bridge = CoinPJ.__new__(CoinPJ)
    bridge.client, bridge.lock = client, asyncio.Lock()
    bridge.peer = SimpleNamespace(id=77, bot=True)
    bridge.cache, bridge.unavailable_until, bridge.last_sent = {}, 0.0, 0.0
    text = "✨ 1 دلار آمریکا :\n💸 261,764 toman\n🪙 1405/07/26 | 20:59:28"
    response = SimpleNamespace(sender_id=88, reply_to_msg_id=100, id=101, raw_text=text)
    with pytest.raises(ValueError, match="bound"):
        await bridge.quote("USD", Decimal(1))
    bridge.last_sent = 0.0
    response.sender_id = 77
    response.reply_to_msg_id = 99
    with pytest.raises(ValueError, match="bound"):
        await bridge.quote("USD", Decimal(1))
    bridge.last_sent = 0.0
    response.reply_to_msg_id = 100
    assert "261,764 Toman" in await bridge.quote("USD", Decimal(1))
