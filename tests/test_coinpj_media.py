"""CoinPJ userbot photo/caption relaying; no real Telegram credentials or network."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mikachan.userbot import (CurrencyReply, UserbotBridge, _coinpj_photo_name,
                              send_currency_reply)

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg image bytes"
PNG = b"\x89PNG\r\n\x1a\n" + b"png image bytes"


def bridge_for(reply, *, download=JPEG):
    peer = SimpleNamespace(id=999, bot=True)

    class Conversation:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def send_message(self, request):
            assert request == "100 usd"
            return SimpleNamespace(id=100)

        async def get_response(self):
            return reply

    client = SimpleNamespace(
        get_entity=AsyncMock(return_value=peer),
        conversation=lambda *_args, **_kwargs: Conversation(),
        download_media=AsyncMock(return_value=download),
    )
    bridge = UserbotBridge.__new__(UserbotBridge)
    bridge.client = client
    bridge.lock = asyncio.Lock()
    bridge.last_request = 0.0
    bridge.cooldown_until = 0.0
    bridge._ready = AsyncMock()
    return bridge, client


@pytest.mark.asyncio
async def test_coinpj_photo_and_caption_are_both_retrieved():
    answer = SimpleNamespace(id=101, sender_id=999, raw_text="100 USD & ین <1",
                             photo=object(), document=None)
    bridge, client = bridge_for(answer)
    response = await bridge.currency_message("100 usd")
    assert response.photo == JPEG
    assert response.filename == "coinpj.jpg"
    assert not response.photo_failed
    assert "100 USD &amp; ین &lt;1" in response.text
    client.download_media.assert_awaited_once_with(answer, file=bytes)


@pytest.mark.asyncio
async def test_coinpj_image_documents_and_text_only_replies():
    answer = SimpleNamespace(id=101, sender_id=999, raw_text="100 USD",
                             photo=None, document=SimpleNamespace(size=64, mime_type="image/png"))
    bridge, client = bridge_for(answer, download=PNG)
    result = await bridge.currency_message("100 usd")
    assert result.photo == PNG and result.filename == "coinpj.png"

    answer.document = SimpleNamespace(size=64, mime_type="application/pdf")
    result = await bridge.currency_message("100 usd")
    assert result.photo is None and not result.photo_failed
    assert client.download_media.await_count == 1
    assert "100 USD" in await bridge.currency("100 usd")


@pytest.mark.asyncio
async def test_coinpj_photo_failure_keeps_the_quote():
    answer = SimpleNamespace(id=101, sender_id=999, raw_text="100 USD",
                             photo=object(), document=None)
    bridge, client = bridge_for(answer)
    client.download_media.side_effect = RuntimeError("Telegram media fetch failed")
    response = await bridge.currency_message("100 usd")
    assert response.photo is None
    assert response.photo_failed
    assert "100 USD" in response.text


@pytest.mark.asyncio
async def test_currency_media_does_not_download_invalid_sender():
    answer = SimpleNamespace(id=101, sender_id=100, raw_text="100 USD",
                             photo=object(), document=None)
    bridge, client = bridge_for(answer)
    with pytest.raises(ValueError, match="verified"):
        await bridge.currency_message("100 usd")
    client.download_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_currency_rejects_large_image_without_download():
    answer = SimpleNamespace(id=101, sender_id=999, raw_text="100 USD",
                             photo=None, document=SimpleNamespace(
                                 size=20_000_000, mime_type="image/jpeg"))
    bridge, client = bridge_for(answer)
    result = await bridge.currency_message("100 usd")
    assert result.photo is None and result.photo_failed
    client.download_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_coinpj_photo_keeps_short_caption_in_one_telegram_message():
    message = SimpleNamespace(reply_photo=AsyncMock(), reply_document=AsyncMock(), reply=AsyncMock())
    response = CurrencyReply("💱 <b>100 usd</b>\n\nRate 123", photo=JPEG)
    await send_currency_reply(message, "100 usd", response)
    message.reply_photo.assert_awaited_once()
    photo = message.reply_photo.await_args.args[0]
    assert photo.data == JPEG
    assert message.reply_photo.await_args.kwargs["caption"] == response.text
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_coinpj_long_caption_preserves_every_character():
    message = SimpleNamespace(reply_photo=AsyncMock(), reply_document=AsyncMock(), reply=AsyncMock())
    response = CurrencyReply("💱 <b>100 usd</b>\n\n" + "1234" * 350, photo=JPEG)
    await send_currency_reply(message, "100 usd", response)
    assert "100 usd" in message.reply_photo.await_args.kwargs["caption"]
    assert len(message.reply_photo.await_args.kwargs["caption"]) < 1024
    message.reply.assert_awaited_once_with(response.text)


@pytest.mark.asyncio
async def test_coinpj_photo_download_failure_is_disclosed_in_text():
    message = SimpleNamespace(reply_photo=AsyncMock(), reply_document=AsyncMock(), reply=AsyncMock())
    await send_currency_reply(message, "100 usd", CurrencyReply("Rate", photo_failed=True))
    assert "Rate" in message.reply.await_args.args[0]
    assert "could not be retrieved" in message.reply.await_args.args[0]
    message.reply_photo.assert_not_awaited()


def test_coinpj_image_magic_bytes_are_validated():
    assert _coinpj_photo_name(JPEG) == "coinpj.jpg"
    assert _coinpj_photo_name(PNG) == "coinpj.png"
    assert _coinpj_photo_name(b"not an image") is None

@pytest.mark.asyncio
async def test_rate_command_coinpj_adapter_also_returns_photo_and_preserves_legacy_text():
    from decimal import Decimal
    from mikachan.coinpj import CoinPJ

    class RateConversation:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return None
        async def send_message(self, prompt):
            assert prompt == "100 ین"
            return SimpleNamespace(id=100)
        async def get_response(self):
            return SimpleNamespace(
                sender_id=999, reply_to_msg_id=100, id=101,
                raw_text="✨ 100 ین ژاپن :\n\n💸 131,900 toman\n💵 $0.63246527 dollar\n\n🪙 1405/06/10 | 00:35:57",
                photo=object(), document=None,
            )

    client = SimpleNamespace(
        is_connected=lambda: True,
        is_user_authorized=AsyncMock(return_value=True),
        get_entity=AsyncMock(return_value=SimpleNamespace(id=999, bot=True)),
        conversation=lambda *_args, **_kwargs: RateConversation(),
        download_media=AsyncMock(return_value=JPEG),
    )
    rate = CoinPJ.__new__(CoinPJ)
    rate.client = client
    rate.lock = asyncio.Lock()
    rate.peer = SimpleNamespace(id=999, bot=True)
    rate.cache = {}
    rate.unavailable_until = 0.0
    rate.last_sent = 0.0
    quote = await rate.quote_media("JPY", Decimal(100))
    assert quote.photo == JPEG
    assert "131,900 Toman" in quote.text
    assert await rate.quote("JPY", Decimal(100)) == quote.text
    client.download_media.assert_awaited_once()
    # Repeated requests reuse the cached text+photo instead of sending to CoinPJ twice.


@pytest.mark.asyncio
async def test_currency_reply_document_fallback_on_unsupported_photo():
    from aiogram.exceptions import TelegramBadRequest
    message = SimpleNamespace(
        reply_photo=AsyncMock(side_effect=TelegramBadRequest()),
        reply_document=AsyncMock(), reply=AsyncMock(),
    )
    await send_currency_reply(message, "100 usd", CurrencyReply("Rate", JPEG))
    assert message.reply_document.await_args.kwargs["caption"] == "Rate"
    message.reply.assert_not_awaited()
