"""Focused regressions for public poetry, local cards, prices and private alerts."""

import io
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw, ImageFont

from mikachan.config import Settings
from mikachan.follow import FollowService, command_target, public_items, public_profile
from mikachan.ganjoor import Ganjoor, render_poem
from mikachan.market_prices import MarketPrices, MarketQuote, parse_asset_query
from mikachan.now_service import NowService, time_card
from mikachan.store import Store
from mikachan.utilities import Utilities, aqi_label


@pytest.mark.asyncio
async def test_ganjoor_catalog_search_and_source_validation(monkeypatch):
    provider = Ganjoor()
    get = AsyncMock(side_effect=[
        [{"id": 2, "name": "حافظ", "published": True},
         {"id": 3, "name": "سعدی", "published": True}],
        [{"fullTitle": "سعدی » غزل", "plainText": "متنی درباره عشق",
          "fullUrl": "/saadi/ghazal/sh1"}],
    ])
    monkeypatch.setattr(provider, "_get", get)
    assert await provider.poets() == [(2, "حافظ"), (3, "سعدی")]
    assert await provider.poets() == [(2, "حافظ"), (3, "سعدی")]
    assert get.await_count == 1
    poem = await provider.search("عشق")
    assert "https://ganjoor.net/saadi/ghazal/sh1" in render_poem(poem)
    with pytest.raises(ValueError):
        render_poem({"fullTitle": "bad", "plainText": "bad", "fullUrl": "//evil.test"})


def test_explicit_asset_amounts_and_calendar_labels():
    assert parse_asset_query("۱۱۲ دلار") == ("USD", Decimal(112))
    assert parse_asset_query("120000 ین") == ("JPY", Decimal(120000))
    assert parse_asset_query("400 ریال عمان") == ("OMR", Decimal(400))
    assert parse_asset_query("ton") == ("TON", Decimal(1))
    assert parse_asset_query("دلار؟") is None
    assert parse_asset_query("0 دلار") is None
    assert "Unhealthy for sensitive groups" in aqi_label(125)
    card = time_card("Tokyo", "Japan", "Asia/Tokyo", {"gregorian", "jalali", "imperial",
                     "hijri", "japanese", "new_gregorian"},
                     datetime(2026, 10, 18, 12, tzinfo=timezone.utc))
    assert "21:00" in card and "Solar Hijri" in card and "Japan · Reiwa 8" in card
    assert "Lunar Hijri" in card and "Next New Year" in card


@pytest.mark.asyncio
async def test_ton_one_source_is_labeled_and_not_invented(monkeypatch):
    prices = MarketPrices()
    monkeypatch.setattr(prices, "bitpin_ton", AsyncMock(return_value=MarketQuote(
        "Bitpin direct", Decimal("348250"), Decimal("348250"),
        datetime(2026, 10, 18, tzinfo=timezone.utc))))
    text = await prices.amount_card("TON", Decimal(2))
    assert "696,500 Toman" in text and "one source" in text


@pytest.mark.asyncio
async def test_now_prompt_is_bound_to_original_user_and_reply(monkeypatch, tmp_path):
    db = Store(tmp_path / "db.sqlite").db
    now = NowService(db)
    monkeypatch.setattr(now, "city", AsyncMock(return_value=("Tehran", "Iran", "Asia/Tehran")))
    prompt_message = SimpleNamespace(message_id=7)
    request = SimpleNamespace(from_user=SimpleNamespace(id=1), chat=SimpleNamespace(id=-100),
                              reply=AsyncMock(return_value=prompt_message))
    await now.command(request, "")
    async def answer(user, reply_id):
        m = SimpleNamespace(from_user=SimpleNamespace(id=user), chat=SimpleNamespace(id=-100),
                            reply_to_message=SimpleNamespace(message_id=reply_id), text="تهران",
                            reply=AsyncMock(return_value=SimpleNamespace(message_id=8)))
        return await now.reply(m)
    assert not await answer(2, 7)
    assert not await answer(1, 6)
    assert await answer(1, 7)
    assert now.preference(1)["zone"] == "Asia/Tehran"
    assert now.preference(2) is None


@pytest.mark.asyncio
async def test_follow_notification_owner_and_tiktok_avatar_stability(monkeypatch, tmp_path):
    store = Store(tmp_path / "db.sqlite")
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=44)))
    follow = FollowService(store, bot)
    target = command_target("tiktok alice")
    token = follow.db.session(42, 42, target)
    follow.db.bind(token, 10)
    fid = follow.db.save(follow.db.get_session(token, 42, 42, 10))
    row = follow.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await follow.deliver(row, target, "posts", "https://www.tiktok.com/@alice/video/123", "new")
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    names = [button.text for group in markup.inline_keyboard for button in group]
    assert names == ["🔗 Link", "⬇️ Download", "👤 Profile panel", "✅ Thank you"]
    notice_token = markup.inline_keyboard[-1][0].callback_data.split(":")[1]
    assert follow.db.notification(notice_token, 42, 42, 44)
    assert follow.db.notification(notice_token, 43, 42, 44) is None
    assert follow.db.notification(notice_token, 42, 43, 44) is None
    assert follow.db.notification(notice_token, 42, 42, 45) is None
    monkeypatch.setattr("mikachan.follow.validate_dns", lambda _url: None)
    monkeypatch.setattr("mikachan.follow._extract", AsyncMock(return_value=[
        [2, {"user": {"nickname": "Alice", "signature": "Bio",
                        "avatarLarger": "https://cdn.example/rotating-1.jpg"}}]]))
    profile = await public_profile(target)
    assert profile == {"name": "Alice", "bio": "Bio"}
    display = await public_profile(target, display_avatar=True)
    assert display["avatar"] == "https://cdn.example/rotating-1.jpg"
    monkeypatch.setattr("mikachan.follow._extract", AsyncMock(return_value=[
        [2, {"user": {"nickname": "Alice", "signature": "Bio",
                        "avatarLarger": "https://cdn.example/rotating-2.jpg"},
              "file_id": "stable_avatar_123"}]]))
    assert (await public_profile(target))["avatar"] == "stable_avatar_123"
    assert follow.db.remove(fid, 42)
    assert follow.db.notification(notice_token, 42, 42, 44)["url"] is None


@pytest.mark.asyncio
async def test_twitter_text_posts_enabled_and_empty_timeline_flagged(monkeypatch, tmp_path):
    monkeypatch.setattr("mikachan.follow.validate_dns", lambda _url: None)
    extract = AsyncMock(return_value=[[2, {"tweet_id": "123", "content": "text-only update"}]])
    monkeypatch.setattr("mikachan.follow._extract", extract)
    target = command_target("twitter alice")
    assert (await public_items(target))[0][2] == "text-only update"
    assert "extractor.twitter.text-tweets=true" in extract.await_args.args[0]
    store = Store(tmp_path / "db.sqlite")
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=44)))
    follow = FollowService(store, bot)
    token = follow.db.session(42, 42, target)
    follow.db.bind(token, 10)
    fid = follow.db.save(follow.db.get_session(token, 42, 42, 10))
    monkeypatch.setattr("mikachan.follow.public_items", AsyncMock(return_value=[
        ("tweets", "https://x.com/alice/status/123", "old")]))
    row = follow.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await follow.check(row)
    monkeypatch.setattr("mikachan.follow.public_items", AsyncMock(return_value=[]))
    row = follow.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    with pytest.raises(ValueError, match="no public data"):
        await follow.check(row)
    assert bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_tiktok_legacy_avatar_url_rebaselines_without_false_alert(monkeypatch, tmp_path):
    store = Store(tmp_path / "db.sqlite")
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=45)))
    follow = FollowService(store, bot)
    target = command_target("tiktok alice")
    token = follow.db.session(42, 42, target)
    follow.db.bind(token, 10)
    follow.db.db.execute("UPDATE follow_sessions SET features=? WHERE id=?",
                         ('["avatar"]', token))
    fid = follow.db.save(follow.db.get_session(token, 42, 42, 10))
    follow.db.db.execute("INSERT INTO follow_baselines VALUES (?,?)", (fid, "avatar"))
    follow.db.db.execute("UPDATE follows SET profile_data=? WHERE id=?",
                         ('{"avatar":"https://cdn.example/old-signed.jpg"}', fid))
    profile = AsyncMock(return_value={"avatar": "stable_avatar_1"})
    monkeypatch.setattr("mikachan.follow.public_profile", profile)
    row = follow.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await follow.check(row)
    assert bot.send_message.await_count == 0
    assert "stable_avatar_1" in follow.db.db.execute(
        "SELECT profile_data FROM follows WHERE id=?", (fid,)).fetchone()[0]
    profile.return_value = {"avatar": "stable_avatar_2"}
    row = follow.db.db.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone()
    await follow.check(row)
    assert bot.send_message.await_count == 1


def test_ocr_enhancement_recognizes_local_fixture():
    image = Image.new("RGB", (300, 90), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 45)
    draw.text((12, 12), "HELLO", fill="black", font=font)
    output = io.BytesIO()
    image.save(output, format="PNG")
    assert "hello" in Utilities._ocr(output.getvalue(), "eng").lower()


@pytest.mark.asyncio
async def test_removed_utility_commands_are_not_dispatched(tmp_path):
    utilities = Utilities(SimpleNamespace(), Settings(bot_token="123:fake", owner_telegram_id=1,
                                                       allowed_telegram_ids="1"),
                          Store(tmp_path / "db.sqlite").db)
    for command in ("/time", "/image", "/sticker", "/stickerimage", "/gif", "/pack", "/convert"):
        assert await utilities.command(SimpleNamespace(), command, "") is False


@pytest.mark.asyncio
async def test_persian_weather_renders_air_and_three_days(tmp_path):
    utilities = Utilities(SimpleNamespace(), Settings(bot_token="123:fake", owner_telegram_id=1,
                                                       allowed_telegram_ids="1"),
                          Store(tmp_path / "db.sqlite").db)
    async def api(url, _params):
        if "geocoding-api" in url:
            return {"results": [{"name": "Kyoto", "country": "Japan", "country_code": "JP",
                                 "latitude": 35.0, "longitude": 135.0, "timezone": "Asia/Tokyo",
                                 "population": 1_400_000}]}
        if "air-quality" in url:
            return {"current": {"us_aqi": 125}}
        return {"timezone": "Asia/Tokyo", "current": {"time": "2026-10-18T20:30",
            "temperature_2m": 18.8, "precipitation": 0.0, "wind_speed_10m": 3.3,
            "weather_code": 0}, "daily": {"time": ["2026-10-18", "2026-10-19", "2026-10-20"],
            "temperature_2m_min": [18, 17, 16], "temperature_2m_max": [25, 24, 23],
            "precipitation_sum": [0, 2, 3], "weather_code": [0, 61, 71]}}
    utilities.api = api
    message = SimpleNamespace(reply=AsyncMock())
    assert await utilities.command(message, "/weather", "کیوتو")
    card = message.reply.await_args.args[0]
    assert "Kyoto, Japan" in card and "18.8 °C" in card
    assert "Unhealthy for sensitive groups" in card
    assert "20:30 local" in card and "2026/10/20" in card
