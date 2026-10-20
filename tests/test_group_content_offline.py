"""Isolated offline regression tests; load the media/note layer without aiogram network dependencies."""
import importlib.util
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class FakeMessage:
    def __init__(self, chat=-100, user=1, text=None, caption=None, message_id=10,
                 replied=None, **media):
        self.chat = SimpleNamespace(id=chat, type="supergroup")
        self.from_user = SimpleNamespace(id=user)
        self.text, self.caption, self.message_id = text, caption, message_id
        self.reply_to_message = replied
        self.photo = media.get("photo")
        self.entities = media.get("entities")
        self.caption_entities = media.get("caption_entities")
        self.has_protected_content = media.get("protected", False)
        for kind in ("animation", "video", "document", "audio", "voice", "sticker", "video_note", "poll", "contact"):
            setattr(self, kind, media.get(kind))
        self.reply = AsyncMock(return_value=SimpleNamespace(message_id=800))
        self.answer = AsyncMock(return_value=SimpleNamespace(message_id=800))
        self.edit_text = AsyncMock()
        self.edit_reply_markup = AsyncMock()


class FakeCallback:
    def __init__(self, message, data, user=1):
        self.message, self.data = message, data
        self.from_user = SimpleNamespace(id=user)
        self.answer = AsyncMock()


class FakeButton:
    def __init__(self, text, callback_data):
        self.text, self.callback_data = text, callback_data


class FakeMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


class FakeEntity:
    def __init__(self, **values):
        self.values = values

    def model_dump(self, **_):
        return self.values

    @classmethod
    def model_validate(cls, values):
        return cls(**values)


@pytest.fixture
def group_content(tmp_path):
    fake_types = types.ModuleType("aiogram.types")
    for key, value in {
        "CallbackQuery": FakeCallback, "InlineKeyboardButton": FakeButton,
        "InlineKeyboardMarkup": FakeMarkup, "Message": FakeMessage,
        "MessageEntity": FakeEntity,
    }.items():
        setattr(fake_types, key, value)
    spec = importlib.util.spec_from_file_location(
        "group_content_isolated", Path(__file__).parents[1] / "src/mikachan/group_content.py")
    module = importlib.util.module_from_spec(spec)
    original_aiogram = sys.modules.get("aiogram")
    original_types = sys.modules.get("aiogram.types")
    try:
        sys.modules["aiogram"] = types.ModuleType("aiogram")
        sys.modules["aiogram.types"] = fake_types
        spec.loader.exec_module(module)
    finally:
        if original_aiogram is None: sys.modules.pop("aiogram", None)
        else: sys.modules["aiogram"] = original_aiogram
        if original_types is None: sys.modules.pop("aiogram.types", None)
        else: sys.modules["aiogram.types"] = original_types
    db = sqlite3.connect(tmp_path / "test.sqlite", isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE group_notes(chat_id INTEGER, name TEXT, body TEXT, PRIMARY KEY(chat_id,name))")
    db.execute("INSERT INTO group_notes VALUES(-100,'old','Legacy saved')")
    db.execute("CREATE TABLE group_settings(chat_id INTEGER,name TEXT,value TEXT,PRIMARY KEY(chat_id,name))")
    db.execute("INSERT INTO group_settings VALUES(-100,'rules','Legacy rules')")
    store = module.GroupContentStore(db)
    bot = SimpleNamespace(copy_message=AsyncMock(), send_message=AsyncMock(),
        send_photo=AsyncMock(), send_video=AsyncMock(), send_animation=AsyncMock(),
        send_document=AsyncMock(), delete_message=AsyncMock())
    community_store = SimpleNamespace(audit=lambda *args: None)
    community = SimpleNamespace(admin=AsyncMock(return_value=True), store=community_store)
    service = module.GroupContent(bot, store, community)
    return module, db, store, bot, community, service


def test_unicode_and_single_keyword(group_content):
    mod, _, _, _, _, _ = group_content
    assert mod.note_name("@Kir") == "kir"
    assert mod.note_name("@هشدار") == "هشدار"
    assert mod.note_name("@ربع سکه") is None
    assert mod.note_name("@قوانین") is None
    assert mod.note_name("a" * 70) is None


def test_legacy_migration_once_and_delete_persists(group_content):
    mod, db, store, *_ = group_content
    assert store.get(-100, "old")["body"] == "Legacy saved"
    assert store.get(-100, mod.RULES)["body"] == "Legacy rules"
    old = store.get(-100, "old")
    store.delete(-100, old["id"])
    store.del_rules(-100)
    mod.GroupContentStore(db)
    assert store.get(-100, "old") is None
    assert store.get(-100, mod.RULES) is None
    assert store.get(-101, "old") is None


@pytest.mark.asyncio
async def test_setnote_inline_and_keyword(group_content):
    _, _, store, bot, _, service = group_content
    source = FakeMessage(text="/setnote @هشدار This is a long warning")
    assert await service.command(source, "/setnote", "@هشدار This is a long warning")
    assert store.get(-100, "هشدار")["body"] == "This is a long warning"
    assert await service.keyword(FakeMessage(text="هشدار"), "هشدار")
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[1] == "This is a long warning"
    assert not await service.keyword(FakeMessage(text="other"), "other")
    assert not await service.keyword(FakeMessage(text="please هشدار"), "please هشدار")


@pytest.mark.asyncio
async def test_media_note_preserves_source_and_caption(group_content):
    _, _, store, bot, _, service = group_content
    source = FakeMessage(message_id=39, caption="<b>Unchanged / caption</b>",
                         animation=SimpleNamespace(file_id="GIF-file-id"))
    action = FakeMessage(text="/setnote @gif", replied=source)
    await service.command(action, "/setnote", "@gif")
    row = store.get(-100, "gif")
    assert row["source_message"] == 39
    assert row["caption"] == "<b>Unchanged / caption</b>"
    assert row["media_file_id"] == "GIF-file-id"
    await service.keyword(FakeMessage(text="gif"), "gif")
    bot.copy_message.assert_awaited_once_with(-100, -100, 39, reply_to_message_id=10)
    bot.send_animation.assert_not_awaited()


@pytest.mark.asyncio
async def test_media_fallback_original_deleted(group_content):
    _, _, store, bot, _, service = group_content
    bot.copy_message.side_effect = ValueError("source deleted")
    source = FakeMessage(message_id=45, caption="Keep caption & emoji 😁",
                         video=SimpleNamespace(file_id="video-file"))
    await service.command(FakeMessage(replied=source), "/setnote", "@test")
    assert await service.keyword(FakeMessage(text="test"), "test")
    bot.send_video.assert_awaited_once()
    assert bot.send_video.await_args.args == (-100, "video-file")
    assert bot.send_video.await_args.kwargs["caption"] == "Keep caption & emoji 😁"
    assert bot.send_video.await_args.kwargs["parse_mode"] is None


@pytest.mark.asyncio
async def test_rules_reply_exact_and_missing_is_silent(group_content):
    mod, _, store, bot, _, service = group_content
    store.del_rules(-100)
    msg = FakeMessage(text="/rules")
    assert await service.command(msg, "/rules", "")
    bot.copy_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    source = FakeMessage(message_id=44, caption="original rules", photo=[SimpleNamespace(file_id="photo-id")])
    await service.command(FakeMessage(replied=source), "/setrules", "")
    assert store.get(-100, mod.RULES)["source_message"] == 44
    assert await service.keyword(FakeMessage(text="قوانین"), "قوانین")
    bot.copy_message.assert_awaited_once_with(-100, -100, 44, reply_to_message_id=10)


@pytest.mark.asyncio
async def test_rules_only_reply(group_content):
    mod, _, store, bot, _, service = group_content
    store.del_rules(-100)
    await service.command(FakeMessage(), "/setrules", "plain words")
    assert store.get(-100, mod.RULES) is None


@pytest.mark.asyncio
async def test_rules_delete_confirm_yes(group_content):
    mod, _, store, bot, _, service = group_content
    msg = FakeMessage(text="/delrules")
    await service.command(msg, "/delrules", "")
    assert store.get(-100, mod.RULES) is not None
    prompt_markup = msg.reply.await_args.kwargs["reply_markup"]
    cbdata = prompt_markup.inline_keyboard[0][0].callback_data
    q = FakeCallback(FakeMessage(message_id=800), cbdata)
    await service.callback(q, cbdata)
    assert store.get(-100, mod.RULES) is None


@pytest.mark.asyncio
async def test_rules_delete_cancel(group_content):
    mod, _, store, _, _, service = group_content
    msg = FakeMessage(text="/delrules")
    await service.command(msg, "/delrules", "")
    cbdata = msg.reply.await_args.kwargs["reply_markup"].inline_keyboard[0][1].callback_data
    await service.callback(FakeCallback(FakeMessage(message_id=800), cbdata), cbdata)
    assert store.get(-100, mod.RULES) is not None


@pytest.mark.asyncio
async def test_notes_menu_add_edit_toggle_delete(group_content):
    _, _, store, bot, _, service = group_content
    msg = FakeMessage(text="/notes")
    await service.command(msg, "/notes", "")
    markup = msg.reply.await_args.kwargs["reply_markup"]
    add = markup.inline_keyboard[-2][0].callback_data
    await service.callback(FakeCallback(FakeMessage(message_id=800), add), add)
    pending = store.pending(-100, 1)
    assert pending["mode"] == "add"
    await service.pending_message(FakeMessage(message_id=900, text="@Test New note",
        replied=FakeMessage(message_id=800)))
    assert store.get(-100, "test")["body"] == "New note"
    msg2 = FakeMessage(text="/notes")
    await service.command(msg2, "/notes", "")
    markup = msg2.reply.await_args.kwargs["reply_markup"]
    item_cb = [x.callback_data for row in markup.inline_keyboard for x in row
               if "open_" in x.callback_data and "@test" in x.text][0]
    await service.callback(FakeCallback(FakeMessage(message_id=800), item_cb), item_cb)
    token = item_cb.split(":")[1]
    toggle = f"gc:{token}:toggle"
    await service.callback(FakeCallback(FakeMessage(message_id=800), toggle), toggle)
    assert not store.get(-100, "test")["active"]
    delete = f"gc:{token}:delete"
    await service.callback(FakeCallback(FakeMessage(message_id=800), delete), delete)
    assert store.get(-100, "test") is not None
    yes = f"gc:{token}:yes"
    await service.callback(FakeCallback(FakeMessage(message_id=800), yes), yes)
    assert store.get(-100, "test") is None


@pytest.mark.asyncio
async def test_setnote_reply_without_key_uses_snapshot(group_content):
    _, _, store, bot, _, service = group_content
    source = FakeMessage(message_id=33, photo=[SimpleNamespace(file_id="photo-xyz")], caption="no edits")
    msg = FakeMessage(message_id=38, text="/setnote", replied=source)
    await service.command(msg, "/setnote", "")
    pending = store.pending(-100, 1)
    assert pending["mode"] == "name_source"
    assert pending["payload"]
    bot.forward_message = AsyncMock(side_effect=AssertionError("Must never fetch prior source"))
    await service.pending_message(FakeMessage(message_id=50, text="@آلبوم", replied=FakeMessage(message_id=800)))
    assert store.get(-100, "آلبوم")["source_message"] == 33
    assert store.get(-100, "آلبوم")["media_file_id"] == "photo-xyz"
    bot.forward_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_scoped_admin_and_no_ambient_capture(group_content):
    _, _, store, _, community, service = group_content
    community.admin.return_value = False
    m = FakeMessage(text="/setnote @abc forbidden")
    await service.command(m, "/setnote", "@abc forbidden")
    assert store.get(-100, "abc") is None
    community.admin.return_value = True
    store.set_pending(-100, 1, "edit", "old", prompt_message=800)
    assert not await service.pending_message(FakeMessage(text="random chat"))
    assert store.pending(-100, 1) is not None


@pytest.mark.asyncio
async def test_reject_protected_media(group_content):
    _, _, store, _, _, service = group_content
    m = FakeMessage(replied=FakeMessage(message_id=26, text="private", protected=True))
    await service.command(m, "/setnote", "@protected")
    assert store.get(-100, "protected") is None


def test_source_commands_removed_from_dispatcher():
    root = Path(__file__).parents[1] / "src/mikachan"
    for filename in ("bot.py", "community.py"):
        content = (root / filename).read_text()
        for cmd in ("/admin", "/setcmd", "/delcmd", "/delnote", '"/note"'):
            assert cmd not in content, (filename, cmd)

@pytest.mark.asyncio
async def test_callback_rejects_another_person_and_group(group_content):
    _, _, store, _, _, service = group_content
    msg = FakeMessage(text="/notes")
    await service.command(msg, "/notes", "")
    markup = msg.reply.await_args.kwargs["reply_markup"]
    token = markup.inline_keyboard[-1][0].callback_data
    other_person = FakeCallback(FakeMessage(message_id=800), token, user=3)
    assert await service.callback(other_person, token)
    other_person.answer.assert_awaited_once()
    other_group = FakeCallback(FakeMessage(chat=-101, message_id=800), token)
    assert await service.callback(other_group, token)
    other_group.answer.assert_awaited_once()
    assert store.count(-100) == 1  # legacy note, unchanged


@pytest.mark.asyncio
async def test_entities_survive_media_fallback(group_content):
    mod, _, store, bot, _, service = group_content
    bot.copy_message.side_effect = RuntimeError("Original removed")
    source = FakeMessage(message_id=51, caption="Clickable link", photo=[SimpleNamespace(file_id="p")],
       caption_entities=[FakeEntity(type="bold", offset=0, length=9)])
    await service.command(FakeMessage(replied=source), "/setnote", "@img")
    await service.keyword(FakeMessage(text="img"), "img")
    entities = bot.send_photo.await_args.kwargs["caption_entities"]
    assert [ent.values for ent in entities] == [{"type": "bold", "offset": 0, "length": 9}]
    assert bot.send_photo.await_args.kwargs["parse_mode"] is None


@pytest.mark.asyncio
async def test_edit_note_with_media_from_prompt(group_content):
    _, _, store, _, _, service = group_content
    store.save(-100, "warning", {"kind": "text", "body": "old"})
    msg = FakeMessage(text="/notes")
    await service.command(msg, "/notes", "")
    menu = msg.reply.await_args.kwargs["reply_markup"]
    selected = next(btn.callback_data for row in menu.inline_keyboard for btn in row
                    if "@warning" in btn.text)
    await service.callback(FakeCallback(FakeMessage(message_id=800), selected), selected)
    token = selected.split(":")[1]
    edit = f"gc:{token}:edit"
    await service.callback(FakeCallback(FakeMessage(message_id=800), edit), edit)
    assert store.pending(-100, 1)["mode"] == "edit"
    replacement = FakeMessage(message_id=40, caption="final video original caption",
        replied=FakeMessage(message_id=800), video=SimpleNamespace(file_id="newfile"))
    assert await service.pending_message(replacement)
    saved = store.get(-100, "warning")
    assert saved["kind"] == "video"
    assert saved["media_file_id"] == "newfile"
    assert saved["caption"] == "final video original caption"
