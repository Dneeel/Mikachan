"""No media is processed locally; provider answers and callbacks stay bound."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import io

import pytest

from mikachan.external_utilities import ExternalUtilities
from mikachan.userbot import UserbotBridge


def message(*, reply=None):
    return SimpleNamespace(chat=SimpleNamespace(id=-100), from_user=SimpleNamespace(id=42),
                           message_id=55, reply_to_message=reply, reply=AsyncMock())


@pytest.mark.asyncio
async def test_ocr_copies_image_without_caption_and_replies_to_command():
    source = SimpleNamespace(message_id=20, photo=[SimpleNamespace(file_size=100)],
                             document=None, audio=None, voice=None, video_note=None)
    bot = SimpleNamespace(copy_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),
                          delete_message=AsyncMock())
    bridge = SimpleNamespace(staging_chat_id=-200,
        external_request=AsyncMock(return_value=[SimpleNamespace(raw_text="recognized", media=None)]))
    relay = ExternalUtilities(bridge, bot)
    original = message(reply=source)
    assert await relay.command(original, "/ocr", "")
    bot.copy_message.assert_awaited_once_with(-200, -100, 20, caption="")
    bridge.external_request.assert_awaited_once_with("ocr", "", staged_id=77,
                                                     reply_text=None, timeout=65,
                                                     media_data=None, media_name=None)
    original.reply.assert_awaited_once_with("recognized")
    bot.delete_message.assert_awaited_once_with(-200, 77)


@pytest.mark.asyncio
async def test_ocr_without_staging_uses_bounded_memory_only():
    source = SimpleNamespace(message_id=20, photo=[SimpleNamespace(file_size=100,
                              mime_type="image/jpeg")], document=None, audio=None,
                             voice=None, video_note=None)
    async def download(_media, *, destination, timeout):
        assert isinstance(destination, io.BytesIO) and timeout == 45
        destination.write(b"\xff\xd8\xfffixture")
    bot = SimpleNamespace(download=AsyncMock(side_effect=download))
    bridge = SimpleNamespace(staging_chat_id=0,
        external_request=AsyncMock(return_value=[SimpleNamespace(raw_text="read", media=None)]))
    relay = ExternalUtilities(bridge, bot)
    original = message(reply=source)
    assert await relay.command(original, "/ocr", "")
    kwargs = bridge.external_request.await_args.kwargs
    assert kwargs["media_data"].startswith(b"\xff\xd8\xff")
    assert kwargs["media_name"].endswith(".jpg")
    assert kwargs["staged_id"] is None


@pytest.mark.asyncio
async def test_asr_replies_to_forwarded_audio_and_translation_to_text():
    source = SimpleNamespace(message_id=21, photo=None, document=None, audio=None,
                             voice=SimpleNamespace(file_size=300), video_note=None)
    bot = SimpleNamespace(copy_message=AsyncMock(return_value=SimpleNamespace(message_id=80)),
                          delete_message=AsyncMock())
    bridge = SimpleNamespace(staging_chat_id=-200,
        external_request=AsyncMock(return_value=[SimpleNamespace(raw_text="transcript", media=None)]))
    relay = ExternalUtilities(bridge, bot)
    original = message(reply=source)
    assert await relay.command(original, "/asr", "")
    bridge.external_request.assert_awaited_with("util", "/asr", staged_id=80,
                                               reply_text=None, timeout=130,
                                               media_data=None, media_name=None)
    quoted = message(reply=SimpleNamespace(text="Hello", caption=None))
    assert await relay.command(quoted, "/tr", "fa")
    bridge.external_request.assert_awaited_with("util", "/tr fa", staged_id=None,
                                               reply_text="Hello", timeout=65,
                                               media_data=None, media_name=None)


@pytest.mark.asyncio
async def test_style_buttons_are_live_and_bound_to_user_chat_message():
    button = SimpleNamespace(text="Style one", data=b"secret", url=None)
    upstream = SimpleNamespace(id=500, raw_text="Choose a style", buttons=[[button]], media=None)
    next_upstream = SimpleNamespace(id=501, raw_text="Styled result", buttons=[], media=None)
    sent = SimpleNamespace(message_id=90, edit_text=AsyncMock(), delete=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock(return_value=sent))
    bridge = SimpleNamespace(external_request=AsyncMock(return_value=[upstream]),
                             external_click=AsyncMock(return_value=[next_upstream]))
    relay = ExternalUtilities(bridge, bot)
    assert await relay.command(message(), "/style", "some text")
    token = next(iter(relay.sessions))
    denied = SimpleNamespace(data=f"uh:{token}:0", from_user=SimpleNamespace(id=7),
                             message=SimpleNamespace(chat=SimpleNamespace(id=-100), message_id=90),
                             answer=AsyncMock())
    assert await relay.callback(denied)
    bridge.external_click.assert_not_awaited()
    denied.answer.assert_awaited_with("Style menu expired or belongs to another user")
    accepted = SimpleNamespace(data=f"uh:{token}:0", from_user=SimpleNamespace(id=42),
                               message=SimpleNamespace(chat=SimpleNamespace(id=-100),
                                                       message_id=90, edit_text=AsyncMock()),
                               answer=AsyncMock())
    assert await relay.callback(accepted)
    bridge.external_click.assert_awaited_once_with(500, 0, 0, "Style one")
    accepted.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_userbot_required_and_pronunciation_rejects_nonenglish_word():
    relay = ExternalUtilities(None, None)
    original = message()
    assert await relay.command(original, "/pron", "سلام")
    assert "user account session" in original.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_ocr_provider_requires_exact_bot_identity():
    bridge = UserbotBridge.__new__(UserbotBridge)
    bridge.external_peers = {}
    bridge.client = SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace(
        bot=True, username="OCRnerdBOT", id=999)))
    with pytest.raises(ValueError, match="identity"):
        await bridge._external_peer("ocr")
    bridge.client.get_entity.return_value.id = 8931851074
    assert (await bridge._external_peer("ocr")).id == 8931851074


@pytest.mark.asyncio
async def test_utilhub_identity_uses_fresh_telegram_resolution():
    class Client:
        get_entity = AsyncMock()

        async def __call__(self, request):
            assert request.username == "UtilHubBot"
            return SimpleNamespace(peer=SimpleNamespace(user_id=77),
                                   users=[SimpleNamespace(id=77, bot=True, username=None)])

    bridge = UserbotBridge.__new__(UserbotBridge)
    bridge.external_peers = {}
    bridge.client = Client()
    assert (await bridge._external_peer("util")).id == 77
    bridge.client.get_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_pronunciation_small_audio_delivers_without_staging_group():
    bot = SimpleNamespace(send_audio=AsyncMock())
    bridge = SimpleNamespace(staging_chat_id=0,
        client=SimpleNamespace(download_media=AsyncMock(return_value=b"ID3audio")))
    relay = ExternalUtilities(bridge, bot)
    original = message()
    item = SimpleNamespace(raw_text="Pronunciation · test", media=object(),
                           file=SimpleNamespace(size=8))
    await relay._text_results(original, [item], kind="util", audio=True)
    bot.send_audio.assert_awaited_once()
    assert bot.send_audio.await_args.kwargs["caption"] == ""


@pytest.mark.asyncio
async def test_media_edit_filters_provider_ad_buttons_and_strips_output():
    from mikachan.external_utilities import ExternalUtilities

    menu = SimpleNamespace(id=101, sender_id=77, media=None,
        buttons=[[SimpleNamespace(text="تبدیل به ویدیو", data=b"video", url=None)],
                 [SimpleNamespace(text="دانلود از اینستاگرام", data=None,
                                  url="https://example.com")],
                 [SimpleNamespace(text="دریافت لینک دانلود", data=b"link", url=None)]])
    result = SimpleNamespace(id=102, sender_id=77, media=object(),
                             raw_text="@AbzarMediaBot", buttons=None)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=50)),
                          copy_message=AsyncMock(), delete_message=AsyncMock())
    bridge = SimpleNamespace(staging_chat_id=-200, external_peers={"edit":
        SimpleNamespace(id=77)}, external_stage=AsyncMock(return_value=60),
        instagram_unstage=AsyncMock(), external_click=AsyncMock(return_value=[result]))
    relay = ExternalUtilities(bridge, bot)
    await relay._edit_result(-100, 42, 10, [menu])
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "تبدیل به ویدیو" in labels
    assert not any("اینستاگرام" in label or "لینک دانلود" in label for label in labels)
    token = next(iter(relay.edit_sessions))
    query = SimpleNamespace(data=f"ed:{token}:0", from_user=SimpleNamespace(id=42),
                            message=SimpleNamespace(chat=SimpleNamespace(id=-100), message_id=50),
                            answer=AsyncMock())
    assert await relay.callback(query)
    bridge.external_click.assert_awaited_once_with(101, 0, 0, "تبدیل به ویدیو", kind="edit")
    kwargs = bot.copy_message.await_args.kwargs
    assert kwargs["caption"] == "" and kwargs["reply_markup"] is None
    bot.delete_message.assert_awaited_once_with(-100, 50)


@pytest.mark.asyncio
async def test_media_edit_stages_replied_sticker_without_invalid_caption():
    source = SimpleNamespace(message_id=20, document=None, audio=None, voice=None,
        video=None, video_note=None, animation=None, photo=None,
        sticker=SimpleNamespace(file_size=50_000, mime_type="image/webp"))
    bot = SimpleNamespace(copy_message=AsyncMock(return_value=SimpleNamespace(message_id=77)))
    relay = ExternalUtilities(SimpleNamespace(staging_chat_id=-200), bot)
    staged, data, name = await relay._stage_reply(message(reply=source), any_media=True)
    assert (staged, data, name) == (77, None, None)
    assert "caption" not in bot.copy_message.await_args.kwargs


@pytest.mark.asyncio
async def test_utility_reply_requires_verified_sender():
    bridge = UserbotBridge.__new__(UserbotBridge)
    peer = SimpleNamespace(id=77)
    other = SimpleNamespace(id=101, sender_id=9, raw_text="spoofed", media=None,
                            buttons=None)
    bridge.client = SimpleNamespace(get_messages=AsyncMock(return_value=[other]))
    with pytest.raises(ValueError, match="did not finish"):
        await bridge._external_wait(peer, 100, timeout=0.1)
