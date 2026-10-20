"""Regressions for the reported Telegram navigation and download choices."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from mikachan.bot import App, collection_view, download_prompt_markup
from mikachan.follow import FollowService, FollowStore, _gallery_items, command_target
from mikachan.podcasts import resolve_episode_link
from mikachan.store import Store


def button_data(markup):
    return {button.text: button.callback_data for row in markup.inline_keyboard
            for button in row if button.callback_data}


def test_youtube_prompt_asks_quality_and_collections_can_close():
    buttons = button_data(download_prompt_markup("request", "https://youtu.be/abcdefghijk"))
    assert buttons["✅ Yes"] == "c:request:q"
    body, menu = collection_view("cid", "Podcast", [{"title": "Episode 1"}], 0,
                                 {"search_sid": "sid"})
    assert "Podcast" in body
    assert "◀ Back to result" in button_data(menu)
    assert "✖ Close" in button_data(menu)


@pytest.mark.asyncio
async def test_youtube_quality_choice_is_bound_to_confirmed_job(tmp_path):
    store = Store(tmp_path / "bot.sqlite")
    url = "https://www.youtube.com/watch?v=abcdefghijk"
    cid = store.confirm_create(42, 42, 100, 0, url, 900)
    assert store.claim_prompt(cid)
    store.set_prompt(cid, 101)
    edits = []

    async def edit_text(body, **kwargs):
        edits.append((body, kwargs))

    async def answer(_text=None):
        return None

    q = SimpleNamespace(from_user=SimpleNamespace(id=42),
                        message=SimpleNamespace(chat=SimpleNamespace(id=42, type="private"),
                                                message_id=101, edit_text=edit_text),
                        answer=answer, data=f"c:{cid}:q")
    app = App.__new__(App)
    app.store = store
    app.authorized = lambda *_args: True
    await app.callback(q)
    assert {"🎬 360p", "🎬 480p", "🎬 720p", "🎬 Up to 1080p", "🎵 MP3"} <= set(
        button_data(edits[0][1]["reply_markup"]))
    q.data = f"c:{cid}:1080"
    await app.callback(q)
    jobs = store.jobs(42)
    assert len(jobs) == 1
    assert store.job_mode(jobs[0]["id"]) == "1080"
    assert store.decision(cid, 42, 42, 101, True)[0] == "ALREADY"


@pytest.mark.asyncio
async def test_podcast_collection_back_returns_to_search_result(tmp_path):
    store = Store(tmp_path / "bot.sqlite")
    sid = store.create_search(42, 42, "Sample", {"podcast"}, {"apple_podcasts"})
    store.set_search_message(sid, 10)
    store.save_results(sid, 0, [{"kind": "podcast", "source": "apple_podcasts",
                                 "title": "Sample", "creator": "Creator",
                                 "url": "https://podcasts.apple.com/us/podcast/sample/id123",
                                 "id": "123"}])
    cid = store.create_collection(42, 42, "Sample", [{"title": "Episode"}],
                                  {"search_sid": sid, "search_index": 0,
                                   "search_generation": 0})
    store.db.execute("UPDATE collections SET message_id=20 WHERE id=?", (cid,))
    edits = []
    async def edit_text(body, **kwargs):
        edits.append((body, kwargs))
    async def remove_markup(**_kwargs):
        return None
    async def answer(_text=None):
        return None
    app = App.__new__(App)
    app.store = store
    app.bot = SimpleNamespace(edit_message_text=edit_text,
                              edit_message_reply_markup=remove_markup)
    app.settings = SimpleNamespace(search_page_size=10)
    app.authorized = lambda *_args: True
    q = SimpleNamespace(data=f"v:{cid}:back:0", from_user=SimpleNamespace(id=42),
                        message=SimpleNamespace(chat=SimpleNamespace(id=42, type="private"),
                                                message_id=20), answer=answer)
    await app.callback(q)
    assert store.search(sid, 42, 42)["message_id"] == 20
    assert "Sample" in edits[-1][0]
    assert "◀ Back" in button_data(edits[-1][1]["reply_markup"])


def test_existing_follow_can_change_categories_without_moving_destination(tmp_path):
    store = Store(tmp_path / "bot.sqlite")
    follows = FollowStore(store.db)
    token = follows.session(42, -100, command_target("tiktok alice"))
    follows.bind(token, 11)
    follows.db.execute("UPDATE follow_sessions SET destination='here' WHERE id=?", (token,))
    follow_id = follows.save(follows.get_session(token, 42, -100, 11))
    edit = follows.edit_session(42, 42, follow_id)
    follows.bind(edit, 12)
    follows.db.execute("UPDATE follow_sessions SET features=?,mode=? WHERE id=?",
                       (json.dumps(["stories", "bio"]), "media", edit))
    assert follows.save(follows.get_session(edit, 42, 42, 12)) == follow_id
    saved = follows.db.execute("SELECT * FROM follows WHERE id=?", (follow_id,)).fetchone()
    assert saved["chat_id"] == -100
    assert json.loads(saved["features"]) == ["stories", "bio"]
    assert saved["mode"] == "notice"


@pytest.mark.asyncio
async def test_following_check_now_and_close_controls(tmp_path):
    store = Store(tmp_path / "bot.sqlite")
    edits = []
    async def edit_text(body, **kwargs):
        edits.append((body, kwargs))
    async def answer(_text=None):
        return None
    bot = SimpleNamespace(edit_message_text=edit_text, send_message=AsyncMock())
    app = App.__new__(App)
    app.store = store
    app.bot = bot
    app.follow = FollowService(store, bot)
    app.authorized = lambda *_args: True
    token = app.follow.db.session(42, 42, command_target("twitter alice"))
    app.follow.db.bind(token, 11)
    fid = app.follow.db.save(app.follow.db.get_session(token, 42, 42, 11))
    store.db.execute("UPDATE follows SET next_check=9999999999 WHERE id=?", (fid,))
    message = SimpleNamespace(chat=SimpleNamespace(id=42, type="private"),
                              message_id=22, edit_text=edit_text)
    app.follow.db.bind_menu(42, 42, 22)
    q = SimpleNamespace(data=f"fm:{fid}:open", from_user=SimpleNamespace(id=42),
                        message=message, answer=answer)
    q.from_user = SimpleNamespace(id=43)
    await app.callback(q)
    assert edits == []
    q.from_user = SimpleNamespace(id=42)
    await app.callback(q)
    actions = button_data(edits[-1][1]["reply_markup"])
    assert {"⚙️ Settings", "🔄 Check now", "✖ Close"} <= actions.keys()
    q.data = actions["🔄 Check now"]
    app.follow.check = AsyncMock(return_value={"timeline": False, "profile": True})
    await app.callback(q)
    assert "Unavailable from public sources: timeline" in bot.send_message.await_args.args[1]
    q.data = "fm:close:close"
    await app.callback(q)
    assert edits[-1][0] == "Following menu closed."


@pytest.mark.asyncio
async def test_following_menu_checks_all_owned_profiles(tmp_path):
    store = Store(tmp_path / "bot.sqlite")
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=22)))
    app = App.__new__(App)
    app.store = store
    app.bot = bot
    app.follow = FollowService(store, bot)
    app.authorized = lambda user, chat, _kind: user == 42 and chat == 42
    for handle in ("alice", "bob"):
        token = app.follow.db.session(42, 42, command_target(f"twitter {handle}"))
        app.follow.db.bind(token, 11)
        app.follow.db.save(app.follow.db.get_session(token, 42, 42, 11))
    await app.show_following(42, 42, "private")
    menu = button_data(bot.send_message.await_args.kwargs["reply_markup"])
    assert menu["🔄 Check now · all"] == "fm:all:check"
    app.follow.check = AsyncMock(return_value={"timeline": True})
    await app.check_all_following(42, 42, "private")
    assert app.follow.check.await_count == 2
    assert "Accessible: 2" in bot.send_message.await_args.args[1]


def test_tiktok_story_listing_uses_extractable_item_url():
    target = command_target("tiktok alice")
    items = _gallery_items([[2, {"id": "123456", "description": "hello"}]],
                           target, "stories")
    assert items == [("stories", "https://www.tiktok.com/@alice/video/123456", "hello")]


@pytest.mark.asyncio
async def test_dead_podcast_enclosure_is_rejected_before_queueing():
    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "open.spotify.com":
            return httpx.Response(200, text=(
                '<meta property="og:title" content="Sample episode">'
                '<meta property="og:description" content="Sample show · Episode">'))
        if request.url.host == "itunes.apple.com":
            return httpx.Response(200, json={"results": [{
                "trackName": "Sample episode", "collectionName": "Sample show",
                "episodeUrl": "https://example.org/deleted.mp3"}]})
        return httpx.Response(404)

    with (patch("mikachan.podcasts.validate_dns"),
          patch("mikachan.podcasts.httpx.AsyncClient",
                side_effect=lambda **kw: original_client(
                    transport=httpx.MockTransport(handler), **kw))):
        with pytest.raises(ValueError, match="no longer available"):
            await resolve_episode_link("https://open.spotify.com/episode/123")
