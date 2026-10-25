"""Regression: owner registry buttons must reach their person/group panels."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mikachan.now_service import NowService
from mikachan.settings_ui import SettingsUI
from mikachan.store import Store


class FakeMessage:
    """Only the fields used by SettingsUI.callback are needed."""

    def __init__(self, chat_id: int, message_id: int = 55):
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.message_id = message_id
        self.edit_text = AsyncMock()
        self.edit_reply_markup = AsyncMock()
        self.answer = AsyncMock()


@pytest.mark.asyncio
async def test_owner_people_group_navigation_and_identity_page(tmp_path, monkeypatch):
    # Keep the core SettingsUI behavior real; only Telegram message methods are mocked.
    monkeypatch.setattr("mikachan.settings_ui.Message", FakeMessage)
    store = Store(tmp_path / "mikachan.sqlite")
    store.seed_access({1, 2})
    store.seed_groups({-100123456789})
    store.bootstrap_access(1)
    assert store.access_registry is not None
    access = store.access_registry
    access.update_person(1, 2, name="Second person")
    ui = SettingsUI(store.db, access, NowService(store.db))
    token = "test-token"
    store.db.execute(
        "INSERT INTO settings_sessions(token,user_id,chat_id,message_id,page,expires) "
        "VALUES (?,?,?,?,'people',strftime('%s','now') + 900)",
        (token, 1, 1, 55),
    )
    msg = FakeMessage(chat_id=1)
    query = SimpleNamespace(from_user=SimpleNamespace(id=1), message=msg,
                            answer=AsyncMock())

    for button_action, expected_page, expected_label in (
        ("person:2", "person:2", "Second person"),
        ("people", "people", "People Registry"),
        ("group:-100123456789", "group:-100123456789", "Assigned Admin IDs"),
        ("groups", "groups", "Group Registry"),
    ):
        await ui.callback(query, f"z:{token}:{button_action}")
        assert store.db.execute("SELECT page FROM settings_sessions WHERE token=?",
                                (token,)).fetchone()[0] == expected_page
        assert expected_label in msg.edit_text.await_args.args[0]
        msg.edit_text.reset_mock()

    await ui.callback(query, f"z:{token}:home")
    msg.edit_text.reset_mock()
    await ui.callback(query, f"z:{token}:identity")
    assert "Nicknames stay within the group" in msg.edit_text.await_args.args[0]
    assert store.db.execute("SELECT page FROM settings_sessions WHERE token=?",
                            (token,)).fetchone()[0] == "identity"


@pytest.mark.asyncio
async def test_owner_registry_rejects_invalid_identifiers(tmp_path, monkeypatch):
    monkeypatch.setattr("mikachan.settings_ui.Message", FakeMessage)
    store = Store(tmp_path / "mikachan.sqlite")
    store.seed_access({1})
    store.bootstrap_access(1)
    ui = SettingsUI(store.db, store.access_registry, NowService(store.db))
    store.db.execute(
        "INSERT INTO settings_sessions(token,user_id,chat_id,message_id,page,expires) "
        "VALUES ('x',1,1,55,'people',strftime('%s','now')+900)")
    msg = FakeMessage(chat_id=1)
    q = SimpleNamespace(from_user=SimpleNamespace(id=1), message=msg,
                        answer=AsyncMock())
    for action in ("person:notanumber", "group:+123", "group:1", "person:-2"):
        await ui.callback(q, f"z:x:{action}")
        msg.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_person_private_chat_and_group_suspend_controls(tmp_path, monkeypatch):
    monkeypatch.setattr("mikachan.settings_ui.Message", FakeMessage)
    store = Store(tmp_path / "state.sqlite")
    store.seed_access({1, 2})
    store.seed_groups({-100})
    store.bootstrap_access(1)
    assert store.access_registry is not None
    access = store.access_registry
    ui = SettingsUI(store.db, access, NowService(store.db))
    store.db.execute(
        "INSERT INTO settings_sessions(token,user_id,chat_id,message_id,page,expires) "
        "VALUES ('abc',1,1,55,'person:2',strftime('%s','now') + 900)")
    msg = FakeMessage(chat_id=1)
    q = SimpleNamespace(from_user=SimpleNamespace(id=1), message=msg,
                        answer=AsyncMock())
    assert not access.private_enabled(2)
    await ui.callback(q, "z:abc:private")
    assert "private" in msg.edit_text.await_args.args[0]
    await ui.callback(q, "z:abc:confirm")
    assert access.private_enabled(2)
    await ui.callback(q, "z:abc:groups")
    await ui.callback(q, "z:abc:group:-100")
    await ui.callback(q, "z:abc:suspend")
    await ui.callback(q, "z:abc:confirm")
    assert store.db.execute("SELECT status FROM group_registry WHERE chat_id=-100").fetchone()[0] == "SUSPENDED"
    assert store.db.execute("SELECT page FROM settings_sessions WHERE token='abc'").fetchone()[0] == "group:-100"
