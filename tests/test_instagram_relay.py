from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path
import asyncio
import shutil
import subprocess

import pytest

from mikachan.instagram_relay import (InstagramRelay, clean_instagram_caption,
                                      instagram_media_link, creator_handle, _unsupported_response)
from mikachan.config import Settings
from mikachan.jobs import Worker
from mikachan.store import Store
from mikachan.userbot import UserbotBridge


def button(label, *, data=b"a"):
    return SimpleNamespace(text=label, data=data)


def card():
    return SimpleNamespace(id=20, raw_text=(
        "🆔 Username: cnn\n✏️ Biography:\n"
        "Asking the hard questions and bringing unique perspective.\n\n"
        "🧍‍♂️ Name: CNN\n👫 Followers: 23805906\n"
        "👫 Following: 324\n🔔 Posts: 30548\n📋 Page status: Public"),
        buttons=[[button("Download stories")],
                 [button("Download highlights"), button("Download posts")],
                 [button("Find the song")]], photo=object())


def test_caption_and_source_link_are_cleaned_without_losing_post_text():
    raw = ("📸 5/30548 · @cnn\n❤️ 4.1K   💬 977\nReal caption.\n"
           "[🔗 Open post](https://www.instagram.com/p/DetFd25iKlc/)\n"
           "🤖 Downloaded with @instaRoxinoBot")
    cleaned, link = clean_instagram_caption(raw)
    assert cleaned == "📸 5/30548 · @cnn\n❤️ 4.1K   💬 977\nReal caption."
    assert link == "https://www.instagram.com/p/DetFd25iKlc/"
    assert clean_instagram_caption("🤖 Downloaded with @instaRoxinoBot") == ("", None)


def test_creator_identity_needs_explicit_provider_evidence():
    post = SimpleNamespace(raw_text="📸 5/30548 · @cnn\nA post mentioning @someoneelse",
                           buttons=[])
    assert creator_handle([post], "https://www.instagram.com/p/example/") == "cnn"
    assert creator_handle([SimpleNamespace(raw_text="Hello @someoneelse", buttons=[])],
                          "https://www.instagram.com/p/example/") is None
    assert creator_handle([], "https://www.instagram.com/stories/alice/123/") == "alice"


def test_instagram_sound_link_is_not_supported():
    assert instagram_media_link("https://www.instagram.com/reel/ABC123/")
    assert instagram_media_link("https://www.instagram.com/stories/alice/123/")
    assert instagram_media_link("https://www.instagram.com/reels/audio/123/") is None
    assert instagram_media_link("https://elsewhere.example/p/ABC123/") is None
    assert "slow down" in _unsupported_response([
        SimpleNamespace(raw_text="Please slow down and try again later")]).lower()


def test_profile_card_uses_only_selected_controls():
    relay = InstagramRelay(None, None, None, None)
    body = relay._profile_text(card(), "cnn")
    assert "Asking the hard questions" in body
    assert "Followers: <b>23805906</b>" in body
    markup, actions = relay._menu_markup("token", card(), profile_handle="cnn")
    labels = [item.text for row in markup.inline_keyboard for item in row]
    assert labels == ["👀 Follow", "⭕ Stories", "🎞 Highlights", "📸 Posts", "🔗 Open profile"]
    assert set(actions) == {"follow", "stories", "highlights", "posts"}


def test_highlight_pages_and_next_five_keep_close_button():
    relay = InstagramRelay(None, None, None, None)
    highlights = SimpleNamespace(buttons=[
        [button("Travel"), button("Family")],
        [button("⬅ Previous"), button("Next ➡")],
        [button("🎵 Find the song")],
    ])
    markup, actions = relay._menu_markup("t", highlights, highlight_list=True)
    labels = [item.text for row in markup.inline_keyboard for item in row]
    assert labels == ["Travel", "Family", "⬅ Previous", "Next ➡", "✖ Close"]
    assert set(actions) == {"h0", "h1", "previous_page", "next_page"}
    posts = SimpleNamespace(buttons=[[button("📥 Next 5 posts (30543 left)")]])
    markup, actions = relay._menu_markup("t", posts)
    assert [item.text for row in markup.inline_keyboard for item in row] == [
        "📥 Next 5 posts (30543 left)", "✖ Close"]
    assert "next_posts" in actions


@pytest.mark.asyncio
async def test_profile_menu_is_user_and_message_bound():
    bridge = SimpleNamespace(instagram_click=AsyncMock())
    relay = InstagramRelay(bridge, None, None, None)
    markup, actions = relay._menu_markup("token", card())
    relay._remember_menu("token", 7, 70, 100, "cnn", card(), actions)
    query = SimpleNamespace(data=markup.inline_keyboard[0][0].callback_data,
                            from_user=SimpleNamespace(id=8),
                            message=SimpleNamespace(chat=SimpleNamespace(id=70),
                                                    message_id=100, delete=AsyncMock()),
                            answer=AsyncMock())
    assert await relay.callback(query)
    bridge.instagram_click.assert_not_awaited()
    query.data = markup.inline_keyboard[-1][0].callback_data
    query.from_user.id = 7
    assert await relay.callback(query)
    query.message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_highlights_open_as_separate_closable_menu():
    highlights = SimpleNamespace(id=35, raw_text="🎞 Highlights of @cnn — page 2 of 2",
                                 buttons=[[button("Travel")], [button("Next ➡")]],
                                 photo=None, document=None, video=None)
    bridge = SimpleNamespace(instagram_click=AsyncMock(return_value=[highlights]))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=200)))
    relay = InstagramRelay(bridge, bot, None, None)
    _markup, actions = relay._menu_markup("profile", card(), profile_handle="cnn")
    relay._remember_menu("profile", 7, 70, 100, "cnn", card(), actions)
    original = SimpleNamespace(chat=SimpleNamespace(id=70), message_id=100, photo=object(),
                               edit_caption=AsyncMock(), answer=AsyncMock())
    query = SimpleNamespace(data="ir:profile:highlights", from_user=SimpleNamespace(id=7),
                            message=original, answer=AsyncMock())
    assert await relay.callback(query)
    bot.send_message.assert_awaited_once()
    original.edit_caption.assert_not_awaited()
    assert "profile" in relay.menus
    listed = [m for m in relay.menus.values() if m.message == 200]
    assert len(listed) == 1 and "h0" in listed[0].buttons


@pytest.mark.asyncio
async def test_profile_collector_waits_for_card_after_initial_notice():
    bridge = UserbotBridge.__new__(UserbotBridge)
    peer = SimpleNamespace(id=99)
    notice = SimpleNamespace(id=11, sender_id=99, raw_text="Please wait",
                             buttons=[], media=None)
    profile = SimpleNamespace(id=12, sender_id=99, raw_text="🆔 Username: cnn",
                              buttons=[[button("Download posts")]], media=object())
    rounds = 0
    async def get_messages(_peer, **_kwargs):
        nonlocal rounds
        rounds += 1
        return [notice] if rounds == 1 else [profile, notice]
    bridge.client = SimpleNamespace(get_messages=get_messages)
    result = await asyncio.wait_for(bridge._instagram_collect(peer, 10, settle=0.1,
                                                              goal="profile"), 3)
    assert rounds >= 2 and result[-1].id == 12


@pytest.mark.asyncio
async def test_profile_collector_waits_past_partial_reply_quiet_period(monkeypatch):
    bridge = UserbotBridge.__new__(UserbotBridge)
    peer = SimpleNamespace(id=99)
    notice = SimpleNamespace(id=11, sender_id=99, raw_text="Preparing profile",
                             buttons=[], media=None)
    profile = SimpleNamespace(id=12, sender_id=99, raw_text="🆔 Username: cnn",
                              buttons=[[button("Download posts")]], media=object())
    rounds = 0
    async def get_messages(_peer, **_kwargs):
        nonlocal rounds
        rounds += 1
        return [notice] if rounds < 4 else [profile, notice]
    ticks = iter(range(0, 100, 2))
    monkeypatch.setattr("mikachan.userbot.time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr("mikachan.userbot.asyncio.sleep", AsyncMock())
    bridge.client = SimpleNamespace(get_messages=get_messages)
    result = await bridge._instagram_collect(peer, 10, settle=0.1, goal="profile")
    assert rounds == 4
    assert result[-1].id == 12


@pytest.mark.asyncio
async def test_profile_collector_keeps_photo_arriving_after_text_controls():
    bridge = UserbotBridge.__new__(UserbotBridge)
    peer = SimpleNamespace(id=99)
    controls = SimpleNamespace(id=11, sender_id=99, raw_text="🆔 Username: cnn",
                               buttons=[[button("Download posts")]], media=None)
    portrait = SimpleNamespace(id=12, sender_id=99, raw_text="", buttons=[], media=object())
    rounds = 0
    async def get_messages(_peer, **_kwargs):
        nonlocal rounds
        rounds += 1
        return [controls] if rounds == 1 else [portrait, controls]
    bridge.client = SimpleNamespace(get_messages=get_messages)
    result = await asyncio.wait_for(bridge._instagram_collect(
        peer, 10, settle=0.1, goal="profile"), 3)
    assert [item.id for item in result] == [11, 12]


@pytest.mark.asyncio
async def test_story_collector_delivers_first_item_before_batch_finishes():
    bridge = UserbotBridge.__new__(UserbotBridge)
    peer = SimpleNamespace(id=99)
    first = SimpleNamespace(id=11, sender_id=99, raw_text="Story 1", media=object(),
                            buttons=[])
    second = SimpleNamespace(id=12, sender_id=99, raw_text="Story 2", media=object(),
                             buttons=[])
    done = SimpleNamespace(id=13, sender_id=99, raw_text="✅ Highlight downloaded",
                           media=None, buttons=[])
    delivered = []
    rounds = 0
    async def get_messages(_peer, **_kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            return [first]
        assert delivered == [11]
        return [done, second, first]
    async def on_media(item):
        delivered.append(item.id)
    bridge.client = SimpleNamespace(get_messages=get_messages)
    result = await asyncio.wait_for(bridge._instagram_collect(
        peer, 10, settle=1, stop_on_highlight_done=True, on_media=on_media), 3)
    assert delivered == [11, 12]
    assert len(result) == 3


@pytest.mark.asyncio
async def test_posts_collector_keeps_edited_next_button_with_new_media():
    bridge = UserbotBridge.__new__(UserbotBridge)
    peer = SimpleNamespace(id=99)
    original = SimpleNamespace(id=10, sender_id=99, raw_text="✅ Sent 5 posts",
                               buttons=[[button("Old option")]], media=None)
    edited = SimpleNamespace(id=10, sender_id=99, raw_text="✅ Sent 5 posts",
                             buttons=[[button("📥 Next 5 posts (8 left)")]], media=None)
    media = SimpleNamespace(id=11, sender_id=99, raw_text="Post", buttons=[], media=object())
    async def get_messages(_peer, **kwargs):
        return edited if "ids" in kwargs else [media]
    bridge.client = SimpleNamespace(get_messages=get_messages)
    result = await asyncio.wait_for(bridge._instagram_collect(peer, 10, settle=0.1,
                                    goal="posts", watch_edit=original), 3)
    assert [item.id for item in result] == [10, 11]
    assert bridge._instagram_has_button(result[0], "next 5 posts")


@pytest.mark.asyncio
async def test_caption_button_added_after_first_snapshot_is_clicked():
    bridge = UserbotBridge.__new__(UserbotBridge)
    bridge.instagram_transfer_lock = asyncio.Lock()
    bridge.instagram_last_click = 0
    peer = SimpleNamespace(id=99)
    stale = SimpleNamespace(id=11, sender_id=99, raw_text="",
                            buttons=[], media=object())
    current = SimpleNamespace(id=11, sender_id=99, raw_text="",
                              buttons=[[button("Get the caption")]],
                              click=AsyncMock(), media=object())
    edited = SimpleNamespace(id=11, sender_id=99, raw_text="Real caption",
                             buttons=[], media=object())
    calls = 0
    async def get_messages(_peer, **_kwargs):
        nonlocal calls
        calls += 1
        return current if calls == 1 else edited
    bridge.client = SimpleNamespace(get_messages=get_messages)
    bridge._ready = AsyncMock()
    bridge._instagram_peer = AsyncMock(return_value=peer)
    assert (await bridge.instagram_caption(stale)).raw_text == "Real caption"
    current.click.assert_awaited_once_with(0, 0)


@pytest.mark.asyncio
async def test_instagram_wait_does_not_hold_other_userbot_operations():
    bridge = UserbotBridge.__new__(UserbotBridge)
    bridge.lock = asyncio.Lock()
    bridge.instagram_lock = asyncio.Lock()
    bridge.instagram_last_sent = 0
    peer = SimpleNamespace(id=99)
    entered = asyncio.Event()
    release = asyncio.Event()
    async def collect(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return []
    bridge.client = SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(id=10)))
    bridge._ready = AsyncMock()
    bridge._instagram_peer = AsyncMock(return_value=peer)
    bridge._instagram_collect = collect
    task = asyncio.create_task(bridge.instagram_request("@cnn"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        async with asyncio.timeout(1):
            async with bridge.lock:
                pass
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_profile_card_and_photo_can_arrive_as_separate_messages(tmp_path):
    controls = card()
    controls.photo = None
    portrait = SimpleNamespace(id=19, raw_text="", buttons=[], photo=object(),
                               file=SimpleNamespace(size=1000))
    bridge = SimpleNamespace(instagram_request=AsyncMock(return_value=[portrait, controls]),
                             instagram_download=AsyncMock())
    async def download(_message, destination, _limit):
        path = Path(str(destination) + ".jpg")
        path.write_bytes(b"\xff\xd8\xffvalid")
        return path
    bridge.instagram_download.side_effect = download
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=88)))
    relay = InstagramRelay(bridge, bot, SimpleNamespace(data_dir=tmp_path), None)
    await relay.show_profile(70, 7, "cnn")
    assert bot.send_photo.await_args.kwargs["caption"].startswith("🆔 Username:")
    assert next(iter(relay.menus.values())).upstream == controls.id


@pytest.mark.asyncio
async def test_story_photo_uses_clean_caption_and_temporary_storage(tmp_path):
    story = SimpleNamespace(id=55, photo=object(), document=None, video=None,
                            file=SimpleNamespace(size=5),
                            raw_text="🤖 Downloaded with @instaRoxinoBot",
                            buttons=[])
    async def download(_message, destination, _max_bytes):
        path = Path(str(destination) + ".jpg")
        path.write_bytes(b"\xff\xd8\xffminimal")
        return path
    async def prepare(paths, _max_bytes):
        return paths
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=77)))
    relay = InstagramRelay(SimpleNamespace(instagram_caption=AsyncMock(return_value=story),
                                           instagram_download=download), bot,
                           SimpleNamespace(data_dir=tmp_path),
                           SimpleNamespace(_prepare_media=prepare))
    assert await relay._send_media(70, 7, story,
                                   "https://www.instagram.com/stories/alice/123/")
    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["caption"] is None
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert len(buttons) == 1 and buttons[0].text == "🔗 Open stories"
    assert list((tmp_path / "jobs").iterdir()) == []


@pytest.mark.asyncio
async def test_fast_copy_strips_provider_footer_and_removes_staging_message(tmp_path):
    post = SimpleNamespace(id=55, photo=object(), document=None, video=None,
                           file=SimpleNamespace(size=5),
                           raw_text="Real caption\n🤖 Downloaded with @instaRoxinoBot",
                           buttons=[])
    bridge = SimpleNamespace(staging_chat_id=-10055,
        instagram_stage=AsyncMock(return_value=80), instagram_unstage=AsyncMock(),
        instagram_download=AsyncMock())
    bot = SimpleNamespace(copy_message=AsyncMock(return_value=SimpleNamespace(message_id=77)))
    relay = InstagramRelay(bridge, bot, SimpleNamespace(data_dir=tmp_path), None)
    assert await relay._send_media(70, 7, post, "https://www.instagram.com/p/ABC123/",
                                   trusted_batch=True)
    assert bot.copy_message.await_args.kwargs["caption"] == "Real caption"
    bridge.instagram_unstage.assert_awaited_once_with(80)
    bridge.instagram_download.assert_not_awaited()
    assert not (tmp_path / "jobs").exists()


@pytest.mark.asyncio
async def test_next_posts_menu_survives_media_button_in_same_batch():
    next_posts = SimpleNamespace(id=61, photo=None, document=None, video=None,
                                 buttons=[[button("📥 Next 5 posts (30 left)")]])
    post = SimpleNamespace(id=62, photo=object(), document=None, video=None,
                           raw_text="Caption\n🤖 Downloaded with @instaRoxinoBot",
                           buttons=[[button("Find the song")]])
    relay = InstagramRelay(None, None, None, None)
    relay._send_media = AsyncMock(return_value=True)
    count, menu, skipped = await relay._send_results(70, 7, "cnn", [next_posts, post])
    assert (count, skipped) == (1, 0)
    assert menu is next_posts


@pytest.mark.asyncio
async def test_story_callback_sends_first_item_while_provider_is_still_uploading():
    story = SimpleNamespace(id=30, photo=object(), document=None, video=None,
                            raw_text="🤖 Downloaded with @instaRoxinoBot", buttons=[])
    delivered = asyncio.Event()
    async def send_media(*_args, **_kwargs):
        delivered.set()
        return True
    async def click(*_args, **kwargs):
        await kwargs["on_media"](story)
        await asyncio.wait_for(delivered.wait(), 1)
        return [story]
    bridge = SimpleNamespace(instagram_click=AsyncMock(side_effect=click))
    bot = SimpleNamespace(send_message=AsyncMock())
    relay = InstagramRelay(bridge, bot, None, None)
    relay._send_media = AsyncMock(side_effect=send_media)
    _, actions = relay._menu_markup("profile", card(), profile_handle="cnn")
    relay._remember_menu("profile", 7, 70, 100, "cnn", card(), actions)
    query = SimpleNamespace(data="ir:profile:stories", from_user=SimpleNamespace(id=7),
                            message=SimpleNamespace(chat=SimpleNamespace(id=70),
                                                    message_id=100), answer=AsyncMock())
    assert await asyncio.wait_for(relay.callback(query), 2)
    relay._send_media.assert_awaited_once()
    assert bot.send_message.await_args.args[1] == "✅ Done"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["posts", "next_posts"])
async def test_posts_stream_before_upstream_batch_finishes_and_keep_next_five(action):
    post = SimpleNamespace(id=30, photo=object(), document=None, video=None,
                           raw_text="📸 1/20 · @cnn\n🤖 Downloaded with @instaRoxinoBot",
                           buttons=[])
    next_button = SimpleNamespace(id=31, photo=None, document=None, video=None,
                                  raw_text="✅ Sent 5 posts.",
                                  buttons=[[button("📥 Next 5 posts (15 left)")]])
    delivered = asyncio.Event()
    async def send_media(*_args, **_kwargs):
        delivered.set()
        return True
    async def click(*_args, **kwargs):
        assert kwargs["on_media"] is not None
        await kwargs["on_media"](post)
        await asyncio.wait_for(delivered.wait(), 1)
        return [post, next_button]
    bridge = SimpleNamespace(instagram_click=AsyncMock(side_effect=click))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=200)))
    relay = InstagramRelay(bridge, bot, None, None)
    relay._send_media = AsyncMock(side_effect=send_media)
    source = card() if action == "posts" else next_button
    _, actions = relay._menu_markup("profile", source, profile_handle="cnn" if action == "posts" else "")
    relay._remember_menu("profile", 7, 70, 100, "cnn", source, actions)
    query = SimpleNamespace(data=f"ir:profile:{action}", from_user=SimpleNamespace(id=7),
                            message=SimpleNamespace(chat=SimpleNamespace(id=70),
                                                    message_id=100, photo=object()),
                            answer=AsyncMock())
    assert await asyncio.wait_for(relay.callback(query), 2)
    assert any("next_posts" in menu.buttons for menu in relay.menus.values())


@pytest.mark.asyncio
async def test_next_five_posts_can_repeat_for_multiple_pages():
    first = card()
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=lambda *_args, **_kwargs:
        SimpleNamespace(message_id=200 + bot.send_message.await_count)))
    pages = []
    async def click(*_args, **kwargs):
        number = len(pages) + 1
        post = SimpleNamespace(id=100 + number * 2, photo=object(), document=None,
            video=None, raw_text="📸 1/100 · @cnn\n🤖 Downloaded with @instaRoxinoBot", buttons=[])
        next_button = SimpleNamespace(id=101 + number * 2, photo=None, document=None,
            video=None, raw_text="✅ Sent 5 posts.",
            buttons=[[button(f"📥 Next 5 posts ({100 - 5 * number} left)")]])
        await kwargs["on_media"](post)
        pages.append(number)
        return [post, next_button]
    bridge = SimpleNamespace(instagram_click=AsyncMock(side_effect=click))
    relay = InstagramRelay(bridge, bot, None, None)
    relay._send_media = AsyncMock(return_value=True)
    _, actions = relay._menu_markup("profile", first, profile_handle="cnn")
    relay._remember_menu("profile", 7, 70, 100, "cnn", first, actions)
    current_token, current_id, action = "profile", 100, "posts"
    for _ in range(3):
        query = SimpleNamespace(data=f"ir:{current_token}:{action}",
            from_user=SimpleNamespace(id=7),
            message=SimpleNamespace(chat=SimpleNamespace(id=70), message_id=current_id,
                                    photo=object()), answer=AsyncMock())
        assert await relay.callback(query)
        current_token, session = max(((token, session) for token, session in relay.menus.items()
            if session.message > current_id), key=lambda pair: pair[1].message)
        current_id, action = session.message, "next_posts"
    assert pages == [1, 2, 3]


@pytest.mark.asyncio
async def test_confirmed_instagram_video_uses_relay_and_cleans_job_files(tmp_path):
    fixture = tmp_path / "fixture.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=160x90:rate=12", "-t", "0.5", "-c:v", "libx264",
                    str(fixture)], check=True)
    settings = Settings(bot_token="123:abc", owner_telegram_id=42,
                        allowed_telegram_ids="42", data_dir=tmp_path / "data")
    store = Store(settings.data_dir / "bot.sqlite")
    sent = SimpleNamespace(message_id=300, video=SimpleNamespace(file_id="video-id"),
                           document=None, audio=None, photo=None)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=200)),
                          edit_message_text=AsyncMock(), delete_message=AsyncMock(),
                          send_video=AsyncMock(return_value=sent))
    worker = Worker(settings, store, bot)
    async def fetch(_url, directory, _max_bytes, *, audio_only):
        assert not audio_only
        target = directory / "01.mp4"
        shutil.copyfile(fixture, target)
        return [target], "Actual caption"
    worker.instagram_relay = SimpleNamespace(fetch_url=AsyncMock(side_effect=fetch))
    worker._ytdlp = AsyncMock(side_effect=AssertionError("legacy extractor used"))
    url = "https://www.instagram.com/reel/ABC123/"
    jid = store.create_job(42, 42, url)
    assert store.transition(jid, "QUEUED", "RESOLVING")
    await worker._run(jid)
    assert store.job(jid)["state"] == "COMPLETED"
    worker.instagram_relay.fetch_url.assert_awaited_once()
    worker._ytdlp.assert_not_awaited()
    assert not (settings.data_dir / "jobs" / jid).exists()
