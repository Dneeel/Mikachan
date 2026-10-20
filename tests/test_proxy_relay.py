from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
    assert labels == ["🇮🇷 Iran · 82ms", "🔄 Refresh", "🌍 By country"]
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
    assert len([b for row in updated.inline_keyboard for b in row]) == 3
    # The old callback cannot be replayed after a provider menu refresh.
    assert await relay.click(query)
    assert bridge.proxy_click.await_count == 1
