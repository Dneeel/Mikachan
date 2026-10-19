from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from mikachan.config import Settings
from mikachan.store import Store
from mikachan.translator import translate_public
from mikachan.utilities import Utilities


@pytest.mark.asyncio
async def test_public_translation_extracts_all_segments(monkeypatch):
    original = httpx.AsyncClient

    def client(*args, **kwargs):
        return original(*args, transport=httpx.MockTransport(lambda _request:
            httpx.Response(200, json=[[['سلام', 'Hello'], [' دنیا', ' world']]])), **kwargs)

    monkeypatch.setattr("mikachan.translator.httpx.AsyncClient", client)
    assert await translate_public("Hello world", "fa") == "سلام دنیا"


@pytest.mark.asyncio
async def test_translate_command_works_without_optional_libretranslate(monkeypatch, tmp_path):
    settings = Settings(bot_token="123:fake", owner_telegram_id=1,
                        allowed_telegram_ids="1")
    utility = Utilities(SimpleNamespace(), settings, Store(tmp_path / "db.sqlite").db)
    monkeypatch.setattr("mikachan.utilities.translate_public", AsyncMock(return_value="سلام دنیا"))
    message = SimpleNamespace(reply=AsyncMock(), reply_to_message=None)
    assert await utility.command(message, "/translate", "fa Hello world")
    assert message.reply.await_args.args[0] == "Translation · FA\n\nسلام دنیا"


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
