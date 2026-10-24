"""New v2 authorization and observation boundaries."""

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mikachan.groupstatus import GroupStatus, alias, window
from mikachan.community import Community, CommunityStore
from mikachan.now_service import NowService
from mikachan.settings_ui import SettingsUI
from mikachan.store import Store
from mikachan.config import Settings
from mikachan.utilities import Utilities


def registry(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    store.seed_access({1, 2})
    store.seed_groups({-100})
    store.bootstrap_access(1)
    assert store.access_registry is not None
    return store, store.access_registry


def test_legacy_migration_roles_private_and_group_scope(tmp_path):
    store, access = registry(tmp_path)
    assert access.role(1) == "OWNER" and access.role(2) == "USER"
    assert access.can_access(1, 1, "private")
    assert not access.can_access(2, 2, "private")
    assert access.can_access(2, -100, "supergroup")
    assert not access.can_access(2, -101, "supergroup")
    assert access.can_manage(1, -100) and not access.can_manage(2, -100)
    store.bootstrap_access(1)
    assert access.role(2) == "USER"
    with pytest.raises(ValueError, match="primary owner"):
        store.bootstrap_access(9)


def test_registered_admin_grant_suspension_expiry_revocation(tmp_path):
    store, access = registry(tmp_path)
    access.update_person(1, 2, role="ADMIN")
    assert not access.can_manage(2, -100)
    access.assign(1, 2, -100, True)
    assert access.can_manage(2, -100)
    access.update_person(1, 2, status="SUSPENDED", suspended_until=time.time() + 60)
    assert not access.can_access(2, -100, "group")
    assert not access.can_manage(2, -100)
    access.update_person(1, 2, name="Renamed")
    assert not access.can_access(2, -100, "group")
    store.db.execute("UPDATE people SET suspended_until=? WHERE user_id=2", (time.time() - 1,))
    assert access.can_manage(2, -100)
    access.update_person(1, 2, role="USER")
    assert not access.can_manage(2, -100)
    with pytest.raises(ValueError, match="Protected primary"):
        access.update_person(1, 1, status="REVOKED")
    with pytest.raises(ValueError, match="operator approval"):
        access.update_person(1, 2, role="OWNER")
    access.update_person(1, 2, status="REVOKED")
    assert not access.can_access(2, -100, "group")
    assert 2 not in store.allowlist()
    with pytest.raises(ValueError, match="Revoked"):
        access.update_person(1, 2, status="ACTIVE")


def test_group_status_and_admin_scope(tmp_path):
    _store, access = registry(tmp_path)
    access.register(1, 3, "Assigned admin", "ADMIN")
    access.add_group(1, -200, "Second")
    access.assign(1, 3, -100, True)
    assert access.can_manage(3, -100)
    assert not access.can_manage(3, -200)
    access.set_group_status(1, -100, "SUSPENDED")
    assert not access.can_access(1, -100, "group")
    assert not access.can_manage(3, -100)
    access.set_group_status(1, -100, "ACTIVE")
    assert access.can_manage(3, -100)
    settings = CommunityStore(_store.db)
    stats = GroupStatus(_store.db, access)
    settings.set_setting(-100, "groupstatus_user_enabled", "0")
    assert not stats.permitted(2, -100, "group")
    assert stats.permitted(3, -100, "group")


def test_suspension_cancels_queued_jobs(tmp_path):
    store, access = registry(tmp_path)
    jid = store.create_job(2, -100, "https://example.com/media.mp4")
    assert store.job(jid)["state"] == "QUEUED"
    access.update_person(1, 2, status="SUSPENDED")
    assert store.job(jid)["state"] == "CANCELLED"
    assert not access.can_access(2, -100, "group")


@pytest.mark.asyncio
async def test_native_telegram_admin_badge_does_not_grant_group_control(tmp_path):
    store, access = registry(tmp_path)
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(
        status="administrator")), restrict_chat_member=AsyncMock())
    community = Community(bot, CommunityStore(store.db), 1, access)
    assert not await community.admin(-100, 2)
    bot.get_chat_member.assert_not_awaited()
    reply = AsyncMock()
    msg = SimpleNamespace(from_user=SimpleNamespace(id=2),
        chat=SimpleNamespace(id=-100, type="supergroup"),
        reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=9)), reply=reply)
    assert await community.command(msg, "/mute", "67")
    bot.restrict_chat_member.assert_not_awaited()
    access.update_person(1, 2, role="ADMIN")
    access.assign(1, 2, -100, True)
    assert await community.command(msg, "/mute", "67")
    assert bot.restrict_chat_member.await_count == 1
    called = bot.restrict_chat_member.await_args
    assert called.args[:2] == (-100, 9)
    assert 3900 <= called.kwargs["until_date"] - time.time() <= 4100


def message(chat: int, mid: int, *, text=None, photo=None, forwarded=False,
            when=None, sender=999):
    return SimpleNamespace(chat=SimpleNamespace(id=chat, type="supergroup"),
        message_id=mid, from_user=SimpleNamespace(id=sender), bot=SimpleNamespace(id=1234),
        date=when or datetime.now(timezone.utc), text=text, photo=photo,
        forward_origin=object() if forwarded else None)


def test_observed_statistics_dedupe_unknown_users_and_exact_time(tmp_path):
    store, access = registry(tmp_path)
    stats = GroupStatus(store.db, access)
    now = datetime(2026, 10, 19, 0, 10, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Tehran"))
    start, end = window("day", now)
    assert end - start == 600
    assert window("week", now)[1] - window("week", now)[0] == 7 * 86400
    assert window("month", now)[1] - window("month", now)[0] == 30 * 86400
    assert window("annual", now)[1] - window("annual", now)[0] == 365 * 86400
    assert window("year", now, 2024)[0] == datetime(2024, 1, 1,
        tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Tehran")).timestamp()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert stats.observe(message(-100, 11, photo=[object()], forwarded=True, when=past))
    assert not stats.observe(message(-100, 11, photo=[object()], forwarded=True, when=past))
    assert not stats.observe(message(-200, 11, text="unapproved"))
    counts = stats.counts(-100, "all", datetime.now(timezone.utc))
    assert counts["total"] == counts["photo"] == counts["forwarded"] == 1
    assert not access.can_access(999, -100, "supergroup")
    assert "Earlier history is unavailable" in stats.report(-100, "all", datetime.now(timezone.utc))
    assert alias("  آمار   روزانه ") == "day"
    assert alias("آمار ۳۶۵ روزه") == "annual"
    assert alias("لطفا آمار بده") is None


def test_old_observations_roll_into_monthly_history(tmp_path):
    store, access = registry(tmp_path)
    stats = GroupStatus(store.db, access)
    old = datetime(2024, 2, 29, 20, 0, tzinfo=timezone.utc)
    assert stats.observe(message(-100, 55, text="old", forwarded=True, when=old))
    assert store.db.execute("SELECT COUNT(*) FROM observed_group_messages").fetchone()[0] == 0
    assert stats.counts(-100, "all", datetime.now(timezone.utc))["total"] == 1
    assert stats.counts(-100, "year", datetime.now(timezone.utc), 2024)["forwarded"] == 1
    card = stats.report(-100, "year", datetime.now(timezone.utc), 2024)
    assert "02 · 1" in card and "Total Messages:</b> 1" in card


@pytest.mark.asyncio
async def test_reply_id_uses_authentic_sender_and_group_scoped_observations(tmp_path):
    store, access = registry(tmp_path)
    access.add_group(1, -200, "Second")
    stats = GroupStatus(store.db, access)
    person = SimpleNamespace(id=2, full_name="Member <A>", username="member")
    for chat_id, mid in [(-100, 10), (-100, 11), (-200, 12)]:
        msg = message(chat_id, mid, text="hello", sender=2)
        msg.from_user = person
        assert stats.observe(msg)
    reply = AsyncMock(return_value=SimpleNamespace(message_id=99))
    command = SimpleNamespace(chat=SimpleNamespace(id=-100, type="supergroup"),
        from_user=SimpleNamespace(id=1),
        reply_to_message=SimpleNamespace(from_user=person,
            forward_origin=SimpleNamespace(sender_user=SimpleNamespace(id=999))),
        reply=reply)
    await stats.show_identity(command)
    body = reply.await_args.args[0]
    assert "<code>2</code>" in body and "Messages today | 2" in body
    assert "999" not in body and "&lt;A&gt;" in body


def test_observation_schema_migrates_existing_database(tmp_path):
    store, access = registry(tmp_path)
    store.db.execute("CREATE TABLE observed_group_messages (chat_id INTEGER, message_id INTEGER, "
        "observed REAL, category TEXT, forwarded INTEGER, tehran_year INTEGER, "
        "tehran_month INTEGER, PRIMARY KEY(chat_id,message_id))")
    GroupStatus(store.db, access)
    columns = [r[1] for r in store.db.execute("PRAGMA table_info(observed_group_messages)")]
    assert "user_id" in columns


@pytest.mark.asyncio
async def test_personal_settings_identical_and_bound(tmp_path):
    store, access = registry(tmp_path)
    now = NowService(store.db)
    ui = SettingsUI(store.db, access, now)
    access.register(1, 3, "Admin", "ADMIN")
    access.register(1, 4, "User", "USER")
    chat = SimpleNamespace(id=-100, type="supergroup")
    messages = []
    for uid in (3, 4):
        reply = AsyncMock(return_value=SimpleNamespace(message_id=uid + 10))
        await ui.show(SimpleNamespace(from_user=SimpleNamespace(id=uid), chat=chat,
                                      reply=reply))
        messages.append(reply.await_args.kwargs["reply_markup"])
    assert messages[0].model_dump()["inline_keyboard"][0][0]["text"] == \
           messages[1].model_dump()["inline_keyboard"][0][0]["text"]
    assert "Access Control" not in str(messages)
    owner_reply = AsyncMock(return_value=SimpleNamespace(message_id=20))
    await ui.show(SimpleNamespace(from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=1, type="private"), reply=owner_reply))
    buttons = owner_reply.await_args.kwargs["reply_markup"]
    assert len(buttons.inline_keyboard) == 7
    assert "Access Control" in buttons.inline_keyboard[0][0].text
    assert "Modules & Features" not in str(buttons)
    # A button from the Owner's menu cannot be used in another chat or by another actor.
    token = buttons.inline_keyboard[0][0].callback_data.split(":")[1]
    assert ui._session(token, 1, 1, 20)
    assert ui._session(token, 3, 1, 20) is None
    assert ui._session(token, 1, -100, 20) is None


@pytest.mark.asyncio
async def test_broadcast_edits_its_original_progress_message(tmp_path):
    store, access = registry(tmp_path)
    bot = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
    ui = SettingsUI(store.db, access, NowService(store.db), bot)
    await ui._broadcast("session", 1, 1, 17, "<status>", [-100])
    bot.send_message.assert_awaited_once_with(-100, "&lt;status&gt;")
    edit = bot.edit_message_text.await_args
    assert "Sent · 1" in edit.args[0]
    assert edit.kwargs["chat_id"] == 1
    assert edit.kwargs["message_id"] == 17


@pytest.mark.asyncio
async def test_saved_location_keeps_calendar_and_weather_skips_geocoding(tmp_path):
    db = Store(tmp_path / "state.sqlite").db
    now = NowService(db)
    now._save_city(2, ("Tehran", "Iran", "Asia/Tehran", 35.69, 51.42))
    db.execute("UPDATE now_preferences SET fields='[\"jalali\"]' WHERE user_id=2")
    now._save_city(2, ("Isfahan", "Iran", "Asia/Tehran", 32.65, 51.67))
    pref = now.preference(2)
    assert pref["fields"] == '["jalali"]'
    assert pref["latitude"] == 32.65
    now.city = AsyncMock(return_value=("Kyoto", "Japan", "Asia/Tokyo", 35.0, 135.0))
    now._card = AsyncMock()
    await now.command(SimpleNamespace(from_user=SimpleNamespace(id=2)), "Kyoto")
    assert now.preference(2)["city"] == "Isfahan"
    util = Utilities(SimpleNamespace(), Settings(bot_token="123:fake", owner_telegram_id=1,
                                                  allowed_telegram_ids="1"), db)
    params = []
    async def api(url, data):
        params.append((url, data))
        if "air-quality" in url:
            return {"current": {"us_aqi": 42}}
        return {"timezone": "Asia/Tehran", "current": {"time": "2026-10-19T10:00",
            "temperature_2m": 20, "precipitation": 0, "wind_speed_10m": 5,
            "weather_code": 0}, "daily": {"time": ["2026-10-19"] * 3,
            "temperature_2m_min": [15] * 3, "temperature_2m_max": [25] * 3,
            "precipitation_sum": [0] * 3, "weather_code": [0] * 3}}
    util.api = api
    msg = SimpleNamespace(from_user=SimpleNamespace(id=2), reply=AsyncMock())
    assert await util.command(msg, "/weather", "")
    assert all("geocoding" not in url for url, _ in params)
    assert params[0][1]["latitude"] == "32.65"
