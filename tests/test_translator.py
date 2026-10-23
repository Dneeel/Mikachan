from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from mikachan.config import Settings
from mikachan.store import Store
from mikachan.external_utilities import ExternalUtilities
from mikachan.utilities import Utilities


@pytest.mark.asyncio
async def test_translation_routes_only_through_userbot(tmp_path):
    settings = Settings(bot_token="123:fake", owner_telegram_id=1,
                        allowed_telegram_ids="1")
    utility = Utilities(SimpleNamespace(), settings, Store(tmp_path / "db.sqlite").db)
    message = SimpleNamespace(reply=AsyncMock(), reply_to_message=None)
    assert not await utility.command(message, "/translate", "fa Hello world")
    bridge = SimpleNamespace(external_request=AsyncMock(return_value=[
        SimpleNamespace(raw_text="سلام دنیا", media=None)]))
    relay = ExternalUtilities(bridge, None)
    request = SimpleNamespace(chat=SimpleNamespace(id=1), from_user=SimpleNamespace(id=1),
                              reply_to_message=None, reply=AsyncMock())
    assert await relay.command(request, "/tr", "fa Hello world")
    bridge.external_request.assert_awaited_once_with("util", "/tr fa Hello world",
        staged_id=None, reply_text=None, timeout=65, media_data=None, media_name=None)
    request.reply.assert_awaited_once_with("سلام دنیا")


@pytest.mark.asyncio
async def test_reference_rate_uses_daily_public_mirror_after_provider_failures(tmp_path):
    settings = Settings(bot_token="123:fake", owner_telegram_id=1,
                        allowed_telegram_ids="1")
    utility = Utilities(SimpleNamespace(), settings, Store(tmp_path / "db.sqlite").db)

    async def api(url, _params):
        if "currency-api.pages.dev" in url:
            return {"date": "2026-10-19", "usd": {"eur": 0.89}}
        raise httpx.ConnectError("unavailable")

    utility.api = api
    card = await utility.reference_rate("USD", "EUR")
    assert "<b>0.89</b> EUR" in card and "Currency API · daily" in card
