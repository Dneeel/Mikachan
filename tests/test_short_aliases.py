from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mikachan.bot import App
from mikachan.store import Store


@pytest.mark.asyncio
async def test_plain_reply_aliases_use_existing_id_and_link_actions(tmp_path):
    app = App.__new__(App)
    app.store = Store(tmp_path / "state.sqlite")
    app.authorized = lambda *_args: True
    app.module_enabled = lambda *_args: True
    app.group_status = SimpleNamespace(observe=lambda _message: None,
                                       show_identity=AsyncMock(), reply=AsyncMock(return_value=False))
    app.community = SimpleNamespace(content=SimpleNamespace(pending_message=AsyncMock(
        return_value=False)))
    app.command = AsyncMock()
    message = SimpleNamespace(text="Id", chat=SimpleNamespace(id=-100, type="supergroup"),
                              from_user=SimpleNamespace(id=7), reply_to_message=object(),
                              new_chat_members=None)
    for alias in ("Id", "ID", "id"):
        message.text = alias
        await app.message(message)
    assert app.group_status.show_identity.await_count == 3
    message.text = "لینک"
    await app.message(message)
    app.command.assert_awaited_once_with(message, "/url")


@pytest.mark.asyncio
async def test_vpn_and_link_commands_remain_authorized_utility_actions(tmp_path):
    app = App.__new__(App)
    app.store = Store(tmp_path / "state.sqlite")
    app.module_enabled = lambda *_args: True
    app.community = SimpleNamespace(command=AsyncMock(return_value=False))
    app.mikasub = SimpleNamespace(open_user=AsyncMock())
    app.userbot = None
    message = SimpleNamespace(chat=SimpleNamespace(id=7, type="private"),
                              from_user=SimpleNamespace(id=7), reply=AsyncMock(),
                              reply_to_message=object())
    await app.command(message, "/vpn")
    app.mikasub.open_user.assert_awaited_once_with(message)
    await app.command(message, "/link")
    assert "USERBOT_SESSION" in message.reply.await_args.args[0]
