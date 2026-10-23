import asyncio
import io
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import zxingcpp
from PIL import Image

from mikachan.bot import App
from mikachan.community import CommunityStore
from mikachan.config import Settings
from mikachan.personality import Mode, mode_for
from mikachan.security import allowed
from mikachan.store import Store
from mikachan.utilities import (convert_image, convert_table, decode_codes, qr_png,
                                select_place, weather_label)
from mikachan.utilities import Utilities, _rate_text


def test_numeric_access_and_mode():
    users = {1, 2}
    assert allowed(2, -100, "supergroup", users, False, {-100})
    assert not allowed(3, -100, "supergroup", users, False, {-100})
    assert not allowed(2, -101, "supergroup", users, False, {-100})
    assert not allowed(2, -100, "supergroup", users, True, {-100})
    assert mode_for(2, users, {2}) is Mode.MOMMY
    assert mode_for(3, users, {3}) is Mode.NORMAL
    assert mode_for(1, users, {2}) is Mode.NORMAL


def test_group_isolation_audit_and_content_import(tmp_path):
    base = Store(tmp_path / "state.sqlite")
    community = CommunityStore(base.db)
    community.set_setting(-100, "rules", "Group A")
    community.set_item("group_notes", -100, "hello", "A note")
    community.set_nickname(-100, 2, "Ally")
    assert community.setting(-101, "rules") == ""
    assert community.item("group_notes", -101, "hello") is None
    assert community.nickname(-101, 2) is None
    assert community.nickname(2, 2) is None
    community.audit(-100, 1, "rules", None, "updated")
    assert base.db.execute("SELECT count(*) FROM community_audit WHERE chat_id=-101").fetchone()[0] == 0
    community.use(-100, 2, "dice")
    assert base.db.execute("SELECT count FROM feature_usage WHERE chat_id=-100").fetchone()[0] == 1
    assert community.content("joke") is None
    data = [{"kind": "poem", "language": "fa", "body": "TEST-ONLY VERSE",
             "author": "Hafez", "source": "test fixture", "rights": "test-only"}]
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(data))
    assert community.import_content(path) == 1
    assert community.hafez()["body"] == "TEST-ONLY VERSE"
    path.write_text(json.dumps([{**data[0], "rights": ""}]))
    with pytest.raises(ValueError):
        community.import_content(path)


def test_bundled_hafez_catalog_has_real_sources_and_seeds_once(tmp_path):
    catalog = Path(__file__).resolve().parents[1] / "src/mikachan/content/hafez.json"
    entries = json.loads(catalog.read_text(encoding="utf-8"))
    assert len(entries) >= 8
    assert all(entry["source"].startswith("https://fa.wikisource.org/wiki/") and
               "CC BY-SA" in entry["rights"] for entry in entries)
    store = CommunityStore(Store(tmp_path / "state.sqlite").db)
    assert store.seed_hafez(catalog) == len(entries)
    assert store.seed_hafez(catalog) == 0
    assert store.hafez() is not None
    assert store.poets() == ["Hafez"]


def test_owner_group_approval_transaction(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    store.seed_access({1})
    store.seed_groups({-200})
    assert store.groups() == {-200}
    store.db.execute("DELETE FROM allowed_groups")
    store.seed_groups({-200})
    assert store.groups() == set()
    token = store.propose_access(1, 1, -100, "group_add")
    store.bind_admin_prompt(token, 9)
    assert store.apply_access(token, 2, 1, 1, 9) == "expired or unauthorized"
    assert store.groups() == set()
    token = store.propose_access(1, 1, -100, "group_add")
    store.bind_admin_prompt(token, 10)
    assert "complete" in store.apply_access(token, 1, 1, 1, 10)
    assert store.groups() == {-100}
    token = store.propose_access(1, 1, -100, "group_remove")
    store.bind_admin_prompt(token, 11)
    assert "complete" in store.apply_access(token, 1, 1, 1, 11)
    assert store.groups() == set()


def test_owner_provider_choices_persist(tmp_path):
    db = Store(tmp_path / "state.sqlite").db
    settings = Settings(bot_token="123:fake", owner_telegram_id=1, allowed_telegram_ids="1")
    utility = Utilities(SimpleNamespace(), settings, db)
    assert utility.provider("fiat") == "frankfurter"
    utility.set_provider("fiat", "open_er_api")
    utility.set_provider("crypto", "coinpaprika")
    assert Utilities(SimpleNamespace(), settings, db).provider("fiat") == "open_er_api"
    with pytest.raises(ValueError):
        utility.set_provider("fiat", "https://attacker.example")


@pytest.mark.asyncio
async def test_exact_cultural_trigger_and_silent_text(tmp_path):
    app = object.__new__(App)
    app.settings = Settings(bot_token="123:fake", owner_telegram_id=1,
                            allowed_telegram_ids="1", data_dir=tmp_path)
    app.store = Store(tmp_path / "state.sqlite")
    app.store.seed_access({1})
    app.community = SimpleNamespace(command=AsyncMock(return_value=True),
                                    search_reply=AsyncMock(return_value=False))
    async def send(value):
        m = SimpleNamespace(from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1, type="private"), text=value,
            new_chat_members=None, reply_to_message=None, reply=AsyncMock())
        await app.message(m)
    await send("  فال  ")
    app.community.command.assert_awaited_once()
    app.community.command.reset_mock()
    await send("فالی")
    await send("فال بگیر")
    await send("شعر.")
    app.community.command.assert_not_awaited()


def test_real_qr_image_and_image_conversion():
    assert weather_label(0) == "Clear sky"
    payload = "https://example.org/سلام"
    png = qr_png(payload)
    assert Image.open(io.BytesIO(png)).format == "PNG"
    assert decode_codes(png) == [payload]
    img = Image.new("RGBA", (80, 40), (255, 0, 0, 100))
    stream = io.BytesIO()
    img.save(stream, format="PNG")
    webp = convert_image(stream.getvalue(), "webp", sticker=True)
    sticker_image = Image.open(io.BytesIO(webp))
    assert sticker_image.format == "WEBP" and max(sticker_image.size) == 512
    jpeg = convert_image(stream.getvalue(), "jpeg")
    assert Image.open(io.BytesIO(jpeg)).format == "JPEG"
    with pytest.raises(ValueError):
        convert_image(b"fake", "png")


def test_real_table_conversion():
    raw = b"name,count\nAlice,2\nBob,3\n"
    output = convert_table(raw, "csv", "json")
    assert json.loads(output)[1] == {"name": "Bob", "count": "3"}
    restored = convert_table(output, "json", "csv")
    assert b"Bob,3" in restored
    with pytest.raises(ValueError):
        convert_table(b"bad", "pdf", "csv")


def test_weather_city_selection_handles_persian_and_deduplicates():
    places = [
        {"name": "Tehran", "country_code": "IR", "country": "Iran",
         "admin1": "Tehran", "latitude": 35.7, "longitude": 51.4, "population": 8700000},
        {"name": "Tehran", "country_code": "IR", "country": "Iran",
         "admin1": "Tehran", "latitude": 35.8, "longitude": 51.5, "population": 500},
        {"name": "Tehrani", "country_code": "PK", "country": "Pakistan",
         "latitude": 25, "longitude": 66, "population": 1000},
    ]
    chosen, suggestions = select_place(places, "tehran")
    assert chosen is places[0] and len(suggestions) == 2
    esfahan = [{"name": "اصفهان", "country_code": "IR", "latitude": 32.65,
                "longitude": 51.67, "population": 1547164}]
    assert select_place(esfahan, "اصفهان")[0] is esfahan[0]


def test_rate_card_never_displays_invalid_or_rounded_zero():
    assert "0.000000001" in _rate_text("USD", "XYZ", "0.000000001", "test", "now")
    for value in ("0", "NaN", "Infinity", "1e100000"):
        with pytest.raises(ValueError):
            _rate_text("USD", "EUR", value, "test", "now")


@pytest.mark.asyncio
async def test_reference_rate_uses_second_provider_on_connection_error(tmp_path):
    import httpx
    db = Store(tmp_path / "state.sqlite").db
    utility = Utilities(SimpleNamespace(), Settings(bot_token="123:fake", owner_telegram_id=1,
        allowed_telegram_ids="1"), db)
    async def api(url, params):
        if "frankfurter" in url or "coingecko" in url:
            raise httpx.ConnectError("offline")
        if "open.er-api" in url:
            return {"rates": {"EUR": 0.86}, "time_last_update_unix": 1_760_000_000}
        return {"quotes": {"USD": {"price": 92000}}, "last_updated": "2026-10-18"}
    utility.api = api
    assert "open_er_api" in await utility.reference_rate("USD", "EUR")
    assert "coinpaprika" in await utility.reference_rate("BTC", "USD")


@pytest.mark.asyncio
async def test_nasa_sends_bounded_image_and_short_caption(tmp_path):
    db = Store(tmp_path / "state.sqlite").db
    utility = Utilities(SimpleNamespace(), Settings(bot_token="123:fake", owner_telegram_id=1,
        allowed_telegram_ids="1"), db)
    image = Image.new("RGB", (16, 16), "navy")
    output = io.BytesIO()
    image.save(output, "PNG")
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    utility.api = AsyncMock(return_value={"title": "A comet", "date": today,
        "url": "https://apod.nasa.gov/test.png", "media_type": "image",
        "explanation": "A long story. " * 200, "copyright": "NASA"})
    utility.apod_page = AsyncMock(side_effect=ValueError("test fallback"))
    utility.apod_image = AsyncMock(return_value=output.getvalue())
    message = SimpleNamespace(reply=AsyncMock(), reply_photo=AsyncMock())
    await utility._command(message, "/nasa", "")
    message.reply_photo.assert_awaited_once()
    assert len(message.reply_photo.await_args.kwargs["caption"]) < 1024
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_nasa_uses_official_page_video_preview(tmp_path):
    utility = Utilities(SimpleNamespace(), Settings(bot_token="123:fake", owner_telegram_id=1,
        allowed_telegram_ids="1"), Store(tmp_path / "state.sqlite").db)
    utility.api = AsyncMock()
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    utility.apod_page = AsyncMock(return_value={"title": "Real APOD video", "date": today,
        "url": "https://assets.science.nasa.gov/preview.jpeg", "media_type": "video",
        "explanation": "Real feature explanation", "copyright": "NASA", "preview": True})
    utility.apod_image = AsyncMock(return_value=qr_png("valid-image"))
    message = SimpleNamespace(reply=AsyncMock(), reply_photo=AsyncMock())
    await utility._command(message, "/nasa", "")
    caption = message.reply_photo.await_args.kwargs["caption"]
    assert "Real APOD video" in caption and "video preview" in caption
    assert "NASA Science" not in caption
    utility.api.assert_not_awaited()


@pytest.mark.asyncio
async def test_nasa_does_not_send_previous_day_as_todays_picture(tmp_path):
    utility = Utilities(SimpleNamespace(), Settings(bot_token="123:fake", owner_telegram_id=1,
        allowed_telegram_ids="1"), Store(tmp_path / "state.sqlite").db)
    utility.apod_page = AsyncMock(return_value={"title": "Old APOD", "date": "2020-01-01",
        "url": "https://assets.science.nasa.gov/old.jpeg", "media_type": "image",
        "explanation": "Old entry"})
    utility.apod_image = AsyncMock()
    message = SimpleNamespace(reply=AsyncMock(), reply_photo=AsyncMock())
    await utility._command(message, "/nasa", "")
    message.reply_photo.assert_not_awaited()
    utility.apod_image.assert_not_awaited()
    assert "2020-01-01" in message.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_apod_page_parses_official_article(tmp_path, monkeypatch):
    import httpx
    utility = Utilities(SimpleNamespace(), Settings(bot_token="123:fake", owner_telegram_id=1,
        allowed_telegram_ids="1"), Store(tmp_path / "state.sqlite").db)
    today = datetime.now(ZoneInfo("America/New_York")).date()
    html_page = '''<html><head><meta property="og:image"
      content="https://assets.science.nasa.gov/stale-june.jpeg"></head><body>
      <div class="media-detail-hero__media"><iframe src="https://www.youtube.com/embed/x"></iframe>
      <img src="https://assets.science.nasa.gov/current-preview.jpeg"></div>
      <h2 class="display-48">A real sky feature</h2>
      <p class="media-detail-hero__description">Explanation: Real observation.</p>
      <table><tr class="media-detail-hero__meta-row"><th>Date:</th><td>DATE_HERE</td></tr>
      <tr class="media-detail-hero__meta-row"><th>Credit:</th><td>NASA team</td></tr></table>
      </body></html>'''.replace("DATE_HERE", today.strftime("%B %d, %Y"))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html_page)), **kwargs))
    data = await utility.apod_page()
    assert data["date"] == today.isoformat() and data["media_type"] == "video"
    assert data["copyright"] == "NASA team" and data["url"].endswith("current-preview.jpeg")
    assert await utility.apod_page() is data  # One-hour in-memory metadata cache.


def test_interrupted_media_cleanup_only_removes_job_artifacts(tmp_path):
    state = tmp_path / "state"
    jobs = state / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "orphan").mkdir()
    (jobs / "orphan" / "media.mp4").write_bytes(b"data")
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    (jobs / "link").symlink_to(outside)
    app = object.__new__(App)
    app.settings = SimpleNamespace(data_dir=state)
    app._clear_interrupted_media()
    assert not list(jobs.iterdir()) and outside.read_bytes() == b"keep"


def test_qr_decoder_does_not_visit_encoded_url(monkeypatch):
    # A decoded URL is returned as text; no network client is used by this path.
    monkeypatch.setattr(zxingcpp, "read_barcodes", lambda image: [SimpleNamespace(text="https://example.org")])
    assert decode_codes(qr_png("x")) == ["https://example.org"]


def test_tgs_renderer_preserves_motion_and_loop(tmp_path):
    import gzip
    from mikachan.render_tgs import render

    # Test-only synthetic moving rectangle, never imported as production content.
    shape = {"v": "5.5.2", "fr": 10, "ip": 0, "op": 3, "w": 100, "h": 100,
             "nm": "test", "ddd": 0, "assets": [], "layers": [{"ddd": 0, "ind": 1,
             "ty": 4, "nm": "shape", "sr": 1, "ks": {"o": {"a": 0, "k": 100},
             "r": {"a": 0, "k": 0}, "p": {"a": 0, "k": [0, 0, 0]},
             "a": {"a": 0, "k": [0, 0, 0]}, "s": {"a": 0, "k": [100, 100, 100]}},
             "ao": 0, "shapes": [{"ty": "rc", "d": 1, "s": {"a": 0, "k": [30, 30]},
             "p": {"a": 1, "k": [{"t": 0, "s": [20, 20], "e": [60, 60]},
                                      {"t": 2, "s": [60, 60]}]},
             "r": {"a": 0, "k": 0}, "nm": "rectangle"},
             {"ty": "fl", "c": {"a": 0, "k": [1, 0, 0, 1]},
              "o": {"a": 0, "k": 100}, "r": 1, "nm": "fill"}],
             "ip": 0, "op": 3, "st": 0, "bm": 0}]}
    raw = gzip.compress(json.dumps(shape).encode())
    result = render(raw)
    gif = Image.open(io.BytesIO(result))
    assert gif.format == "GIF" and gif.n_frames > 1
    assert gif.info["loop"] == 0
    settings = Settings(bot_token="123:fake", owner_telegram_id=1, allowed_telegram_ids="1")
    util = Utilities(SimpleNamespace(), settings, Store(tmp_path / "state.sqlite").db)
    child_result = asyncio.run(util.tgs_to_gif(raw))
    assert Image.open(io.BytesIO(child_result)).n_frames > 1
    with pytest.raises(ValueError):
        render(b"not TGS")


def test_session_ownership_and_prompt_binding(tmp_path):
    store = CommunityStore(Store(tmp_path / "state.sqlite").db)
    sid = store.new_session(1, -100, "poem")
    store.db.execute("UPDATE community_sessions SET message_id=25 WHERE id=?", (sid,))
    assert store.session(sid, 1, -100, 25)
    assert store.session(sid, 2, -100, 25) is None
    assert store.session(sid, 1, -101, 25) is None
    assert store.session(sid, 1, -100, 26) is None


@pytest.mark.asyncio
async def test_poetry_callback_rejects_other_actor(tmp_path):
    from mikachan.community import Community

    database = CommunityStore(Store(tmp_path / "state.sqlite").db)
    sid = database.new_session(1, -100, "poem")
    database.db.execute("UPDATE community_sessions SET message_id=25 WHERE id=?", (sid,))
    community = Community(SimpleNamespace(), database, 1)
    q = SimpleNamespace(from_user=SimpleNamespace(id=2),
        message=SimpleNamespace(chat=SimpleNamespace(id=-100), message_id=25,
                                answer=AsyncMock(), edit_text=AsyncMock()),
        answer=AsyncMock())
    assert await community.callback(q, f"u:{sid}:random")
    q.answer.assert_awaited_once_with("This menu is unavailable.")
    q.message.answer.assert_not_awaited()
