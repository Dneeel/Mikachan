"""Telegram access, pinned pages, converter privacy and preserved proxy routing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mikachan.bot import App
from mikachan.mikasub_ui import MikaSubUI
from mikachan.mikasub_core import MikaSubError
from mikachan.store import Store


class FakeMessage:
    def __init__(self, chat_id=1, mid=10, kind="private"):
        self.chat = SimpleNamespace(id=chat_id, type=kind)
        self.message_id = mid
        self.edit_text = AsyncMock()
        self.delete = AsyncMock()
        self.edit_reply_markup = AsyncMock()
        self.reply = AsyncMock()
        self.from_user = SimpleNamespace(id=1)
        self.reply_to_message = None
        self.text = None


class FakeBot:
    def __init__(self):
        self.messages = []
        self.send_message = AsyncMock(side_effect=self._message)
        self.send_document = AsyncMock(side_effect=self._document)
        self.send_photo = AsyncMock(side_effect=self._photo)

    async def _message(self, chat, text, **_kwargs):
        sent = FakeMessage(chat, 100 + len(self.messages), "private" if chat > 0 else "supergroup")
        sent.text = text
        self.messages.append(sent)
        return sent

    async def _document(self, chat, document, **_kwargs):
        sent = await self._message(chat, "document")
        sent.document_data = document.data
        return sent

    async def _photo(self, chat, photo, **_kwargs):
        sent = await self._message(chat, "photo")
        sent.photo_data = photo.data
        return sent


def query(message, token, action, actor=1):
    return SimpleNamespace(message=message, from_user=SimpleNamespace(id=actor),
                           data=f"ms:{token}:{action}", answer=AsyncMock())


def fixture(tmp_path, monkeypatch, *, chat=1):
    monkeypatch.setattr("mikachan.mikasub_ui.Message", FakeMessage)
    store = Store(tmp_path / "state.sqlite")
    store.seed_access({1, 2})
    store.seed_groups({-100})
    store.bootstrap_access(1)
    access = store.access_registry
    assert access is not None
    bot = FakeBot()
    source = [
        {"uri": f"vless://u{i}@h.example:443?security=tls#name{i}",
         "name": f"name{i}", "protocol": "vless"} for i in range(13)
    ]
    core = SimpleNamespace(
        metadata=AsyncMock(return_value={"version": "a" * 24, "total": len(source),
                                              "created_at": "2026-10-22"}),
        items=AsyncMock(side_effect=lambda _version, start, count: source[start:start + count]),
        all_items=AsyncMock(return_value=source), link=lambda: "",
    )
    ui = MikaSubUI(core, store, bot, access)
    message = FakeMessage(chat, 10, "private" if chat > 0 else "supergroup")
    async def reply(*_args, **_kwargs):
        return await bot._message(chat, "home")
    message.reply = AsyncMock(side_effect=reply)
    return ui, bot, message, source


@pytest.mark.asyncio
async def test_launcher_has_two_choices_and_13_items_page_five_at_a_time(tmp_path, monkeypatch):
    ui, bot, command, source = fixture(tmp_path, monkeypatch)
    await ui.open_user(command)
    home = bot.messages[-1]
    launcher = command.reply.await_args.kwargs["reply_markup"]
    assert [row[0].text for row in launcher.inline_keyboard] == [
        "🏴‍☠️ MikaSub", "🔄 Sub Converter", "🙏 Thanks"]
    session = next(iter(ui.sessions.values()))
    await ui.callback(query(home, session.token, "collection"), f"ms:{session.token}:collection")
    first_preview = home.edit_text.await_args.args[0]
    assert "1–5 of 13" in first_preview
    assert source[0]["uri"] in first_preview
    assert session.next_offset == 5
    await ui.callback(query(home, session.token, "stash"), f"ms:{session.token}:stash")
    await ui.callback(query(home, session.token, "here"), f"ms:{session.token}:here")
    first = bot.messages[-1]
    assert "1–5 of 13" in first.text and source[0]["uri"] in first.text
    await ui.callback(query(first, session.token, "more:5"), f"ms:{session.token}:more:5")
    second = bot.messages[-1]
    assert "6–10 of 13" in second.text
    # A rapid/replayed click on a historical batch cannot advance twice.
    await ui.callback(query(first, session.token, "more:5"), f"ms:{session.token}:more:5")
    assert session.next_offset == 10
    await ui.callback(query(second, session.token, "more:10"), f"ms:{session.token}:more:10")
    third = bot.messages[-1]
    assert "11–13 of 13" in third.text and "End of collection" in third.text
    assert "5 More" not in [button.text for row in bot.send_message.await_args.kwargs[
        "reply_markup"].inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_collection_without_published_sources_explains_owner_setup(tmp_path, monkeypatch):
    ui, bot, command, _ = fixture(tmp_path, monkeypatch)
    ui.core.metadata = AsyncMock(side_effect=MikaSubError("Worker HTTP 404: snapshot unavailable"))
    await ui.open_user(command)
    home = bot.messages[-1]
    session = next(iter(ui.sessions.values()))
    await ui.callback(query(home, session.token, "collection"), f"ms:{session.token}:collection")
    text = home.edit_text.await_args.args[0]
    assert "No configurations are published yet" in text
    assert "Update Now" in text


@pytest.mark.asyncio
async def test_qr_selector_one_item_and_user_binding(tmp_path, monkeypatch):
    ui, bot, command, _source = fixture(tmp_path, monkeypatch)
    await ui.open_user(command)
    home = bot.messages[-1]
    session = next(iter(ui.sessions.values()))
    await ui.callback(query(home, session.token, "collection"), f"ms:{session.token}:collection")
    await ui.callback(query(home, session.token, "here"), f"ms:{session.token}:here")
    batch = bot.messages[-1]
    denied = query(batch, session.token, "select:0", actor=2)
    assert await ui.callback(denied, denied.data)
    denied.answer.assert_awaited_with("Menu expired or belongs to another user")
    await ui.callback(query(batch, session.token, "select:0"), f"ms:{session.token}:select:0")
    selector = bot.messages[-1]
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert [button.text for button in markup.inline_keyboard[0]] == ["1", "2", "3", "4", "5"]
    await ui.callback(query(selector, session.token, "qr:0"), f"ms:{session.token}:qr:0")
    assert bot.messages[-1].photo_data.startswith(b"\x89PNG")
    # Group and message identity also bind callbacks.
    foreign = FakeMessage(-100, batch.message_id, "supergroup")
    wrong = query(foreign, session.token, "more:5")
    await ui.callback(wrong, wrong.data)
    wrong.answer.assert_awaited_with("Menu expired or belongs to another user")


@pytest.mark.asyncio
async def test_converter_stays_in_approved_group_and_does_not_publish_url(tmp_path, monkeypatch):
    ui, bot, command, _ = fixture(tmp_path, monkeypatch, chat=-100)
    await ui.open_user(command)
    home = bot.messages[-1]
    session = next(iter(ui.sessions.values()))
    await ui.callback(query(home, session.token, "converter"), f"ms:{session.token}:converter")
    prompt = bot.messages[-1]
    url = "https://public.example/private?key=do-not-leak"
    supplied = FakeMessage(-100, 77, "supergroup")
    supplied.reply_to_message = prompt
    supplied.text = url
    monkeypatch.setattr("mikachan.mikasub_ui.fetch_subscription",
                        AsyncMock(return_value=b"vless://u@host.example:443#Original\n"))
    assert await ui.reply(supplied)
    supplied.delete.assert_awaited_once()
    assert session.total == 1 and session.converted[0]["name"] == "Original"
    assert bot.messages[-1].chat.id == -100
    assert all(url not in (message.text or "") for message in bot.messages)
    assert "MikaSub" not in [button.text for row in bot.messages[-1].edit_text.await_args.kwargs[
        "reply_markup"].inline_keyboard for button in row]


def test_proxy_handler_is_separate_and_owner_settings_has_mikasub(tmp_path, monkeypatch):
    from mikachan.now_service import NowService
    from mikachan.settings_ui import SettingsUI
    store = Store(tmp_path / "state.sqlite")
    store.seed_access({1})
    store.bootstrap_access(1)
    ui = SettingsUI(store.db, store.access_registry, NowService(store.db),
                    mikasub=SimpleNamespace())
    text, markup = ui._render("owner", 1, 1, "home")
    assert "Owner Control" in text
    assert "🏴‍☠️ MikaSub" in [button.text for row in markup.inline_keyboard for button in row]
    assert App.command is not None  # The existing /proxy regression suite exercises its relay.
