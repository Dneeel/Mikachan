from types import SimpleNamespace
from unittest.mock import AsyncMock
import io

import pytest
from PIL import Image
import zxingcpp

from mikachan.proxy_relay import ProxyRelay, proxy_link


def button(text, *, url=None, data=None):
    return SimpleNamespace(text=text, url=url, data=data)


def upstream(message_id=10):
    return SimpleNamespace(id=message_id, raw_text="✅ Best proxies available now", buttons=[
        [button("🇮🇷 Iran · 82ms", url="tg://proxy?server=one.example&port=443&secret=abc")],
        [button("🔄 Refresh", data=b"refresh"), button("🌍 By country", data=b"country")],
        [button("⬅ Back", data=b"back")],
        [button("Bad website", url="https://elsewhere.example/phish")],
    ])


def test_only_telegram_proxy_links_are_relayed():
    assert proxy_link("tg://proxy?server=one.example&port=443&secret=abc") == \
        "https://t.me/proxy?server=one.example&port=443&secret=abc"
    assert proxy_link("https://elsewhere.example/phish") is None
    assert proxy_link("https://t.me/joinchat/abc") is None
    assert proxy_link("https://t.me:bad/proxy") is None


@pytest.mark.asyncio
async def test_proxy_panel_keeps_real_links_and_buttons_without_back():
    bridge = SimpleNamespace(proxy_request=AsyncMock(return_value=upstream()),
                             proxy_click=AsyncMock(return_value=(upstream(11), "")))
    relay = ProxyRelay(bridge)
    sent = SimpleNamespace(message_id=123)
    message = SimpleNamespace(from_user=SimpleNamespace(id=7), chat=SimpleNamespace(id=7),
                              reply=AsyncMock(return_value=sent))
    await relay.open(message)
    markup = message.reply.await_args.kwargs["reply_markup"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["🇮🇷 Iran · 82ms", "🔄 Refresh", "🌍 By country",
                      "📋 All proxies as text", "🙏 Thanks"]
    assert "Live options from" not in message.reply.await_args.args[0]
    assert markup.inline_keyboard[0][0].url.startswith("https://t.me/proxy?")
    callback = markup.inline_keyboard[1][0].callback_data
    assert callback.startswith("px:")

    query = SimpleNamespace(data=callback, from_user=SimpleNamespace(id=8),
        message=SimpleNamespace(chat=SimpleNamespace(id=7), message_id=123,
                                edit_text=AsyncMock()), answer=AsyncMock())
    assert await relay.click(query)
    bridge.proxy_click.assert_not_awaited()

    query.from_user.id = 7
    assert await relay.click(query)
    bridge.proxy_click.assert_awaited_once_with(10, 1, 0)
    updated = query.message.edit_text.await_args.kwargs["reply_markup"]
    assert [b.text for row in updated.inline_keyboard for b in row] == [
        "🇮🇷 Iran · 82ms", "🔄 Refresh", "🌍 By country", "⬅ Back",
        "📋 All proxies as text", "🙏 Thanks"]
    # The old callback cannot be replayed after a provider menu refresh.
    assert await relay.click(query)
    assert bridge.proxy_click.await_count == 1


@pytest.mark.asyncio
async def test_proxy_thanks_deletes_bound_message_only():
    relay = ProxyRelay(SimpleNamespace(proxy_request=AsyncMock(return_value=upstream())))
    message = SimpleNamespace(from_user=SimpleNamespace(id=7), chat=SimpleNamespace(id=7),
                              reply=AsyncMock(return_value=SimpleNamespace(message_id=123)))
    await relay.open(message)
    markup = message.reply.await_args.kwargs["reply_markup"]
    thanks = markup.inline_keyboard[-1][0].callback_data
    query = SimpleNamespace(data=thanks, from_user=SimpleNamespace(id=8),
                            message=SimpleNamespace(chat=SimpleNamespace(id=7), message_id=123,
                                                    delete=AsyncMock()), answer=AsyncMock())
    await relay.click(query)
    query.message.delete.assert_not_awaited()
    query.from_user.id = 7
    await relay.click(query)
    query.message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_proxy_text_and_qr_are_bound_and_generated_in_memory():
    six = upstream()
    six.buttons = [[button(f"Country {index} · {80 + index}ms",
                          url=f"tg://proxy?server=host{index}.example&port=443&secret=abc")]
                   for index in range(1, 7)] + six.buttons[1:]
    relay = ProxyRelay(SimpleNamespace(proxy_request=AsyncMock(return_value=six)))
    message = SimpleNamespace(from_user=SimpleNamespace(id=7), chat=SimpleNamespace(id=7),
                              reply=AsyncMock(return_value=SimpleNamespace(message_id=123)))
    await relay.open(message)
    menu = message.reply.await_args.kwargs["reply_markup"]
    list_button = next(b for row in menu.inline_keyboard for b in row
                       if b.text == "📋 All proxies as text")
    text_message = SimpleNamespace(message_id=124, chat=SimpleNamespace(id=7),
                                   answer_photo=AsyncMock(), delete=AsyncMock())
    menu_message = SimpleNamespace(message_id=123, chat=SimpleNamespace(id=7),
                                   answer=AsyncMock(return_value=text_message))
    query = SimpleNamespace(data=list_button.callback_data,
                            from_user=SimpleNamespace(id=7), message=menu_message,
                            answer=AsyncMock())
    assert await relay.click(query)
    assert "https://t.me/proxy?" in menu_message.answer.await_args.args[0]
    text_buttons = menu_message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0]
    query.data = text_buttons[0].callback_data
    query.message = text_message
    query.from_user.id = 8
    await relay.click(query)
    text_message.answer_photo.assert_not_awaited()
    query.from_user.id = 7
    await relay.click(query)
    image_bytes = text_message.answer_photo.await_args.args[0].data
    assert image_bytes.startswith(b"\x89PNG")
    codes = zxingcpp.read_barcodes(Image.open(io.BytesIO(image_bytes)))
    assert len(codes) == 1
    assert codes[0].text.count("https://t.me/proxy?") == 6
    query.data = text_buttons[1].callback_data
    await relay.click(query)
    text_message.delete.assert_awaited_once()
