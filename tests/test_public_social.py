"""Public social adapters must validate identities and sort pinned timelines."""

from unittest.mock import AsyncMock

import pytest

from mikachan.follow import command_target, public_items
from mikachan.jobs import extractor_error, gallery_media_headers
from mikachan.security import source
from mikachan.public_social import public_statuses, public_tiktok_sound, public_x_media


@pytest.mark.asyncio
async def test_x_timeline_recovers_posts_anonymous_gallery_misses(monkeypatch):
    data = {"code": 200, "results": [
        {"id": "2111992580854399438", "author": {"screen_name": "Dniuis"},
         "created_timestamp": 1792373244, "text": "photo"},
        {"id": "2111998764345069830", "author": {"screen_name": "Dniuis"},
         "created_timestamp": 1792374719, "text": "Test"},
        {"id": "2111999999999999999", "author": {"screen_name": "someone_else"},
         "created_timestamp": 1792379999, "text": "not this creator"},
    ]}
    monkeypatch.setattr("mikachan.public_social._get", AsyncMock(return_value=data))
    assert await public_statuses("twitter", "Dniuis") == [
        ("tweets", "https://x.com/Dniuis/status/2111998764345069830", "Test"),
        ("tweets", "https://x.com/Dniuis/status/2111992580854399438", "photo"),
    ]
    monkeypatch.setattr("mikachan.follow.validate_dns", lambda _url: None)
    assert (await public_items(command_target("twitter Dniuis")))[0][2] == "Test"


@pytest.mark.asyncio
async def test_tiktok_pinned_posts_do_not_hide_latest(monkeypatch):
    monkeypatch.setattr("mikachan.public_social._get", AsyncMock(return_value={
        "code": 200, "results": [
            {"id": "7683608687344192781", "created_timestamp": 1788979556,
             "author": {"screen_name": "truereinhard"}, "text": "pinned"},
            {"id": "7698159202811776269", "created_timestamp": 1792367362,
             "author": {"screen_name": "truereinhard"}, "text": "new"},
        ],
    }))
    items = await public_statuses("tiktok", "truereinhard")
    assert items[0][2] == "new"


@pytest.mark.asyncio
async def test_x_media_requires_exact_status_and_author(monkeypatch):
    url = "https://x.com/arabellajoyce7/status/2103632534596632587"
    status = {"id": "2103632534596632587",
              "author": {"screen_name": "arabellajoyce7"},
              "media": {"all": [
                  {"type": "video", "url": "https://video.twimg.com/media/real.mp4"},
                  {"type": "photo", "url": "https://private.example/photo.jpg"},
              ]}}
    get = AsyncMock(return_value={"code": 200, "status": status})
    monkeypatch.setattr("mikachan.public_social._get", get)
    assert await public_x_media(url) == ["https://video.twimg.com/media/real.mp4"]
    status["author"] = {"screen_name": "imposter"}
    with pytest.raises(ValueError, match="author identity"):
        await public_x_media(url)


@pytest.mark.asyncio
async def test_tiktok_sound_uses_exact_music_identity(monkeypatch):
    sound = "7644448809103182623"
    video = "7644448756699647263"
    monkeypatch.setattr("mikachan.public_social.validate_dns", lambda _url: None)
    monkeypatch.setattr("mikachan.public_social._get", AsyncMock(return_value={
        "code": 200, "collection": {"id": sound}, "results": [
            {"id": video, "url": f"https://www.tiktok.com/@artist/video/{video}"}]}))
    import httpx
    original = httpx.AsyncClient

    def client(*args, **kwargs):
        return original(*args, transport=httpx.MockTransport(lambda _request:
            httpx.Response(200, json={"code": 0, "data": {"id": video,
                "music_info": {"id": sound, "play": "https://cdn.example/real.mp3",
                               "duration": "133", "title": "original sound"}}})), **kwargs)

    monkeypatch.setattr("mikachan.public_social.httpx.AsyncClient", client)
    assert await public_tiktok_sound(sound) == (
        "https://cdn.example/real.mp3", "original sound", 133)


def test_gallery_media_headers_preserve_only_safe_extractor_fields():
    assert gallery_media_headers({"_http_headers": {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.instagram.com/",
        "Cookie": "session=secret",
        "Authorization": "Bearer secret",
    }}) == {"User-Agent": "Mozilla/5.0", "Referer": "https://www.instagram.com/"}
    assert gallery_media_headers({"_http_headers": {
        "Referer": "https://attacker.example/", "User-Agent": "bad\ninjection",
    }}) == {}


def test_pornhub_uses_site_extractor_and_disabled_item_is_reported_truthfully():
    url = "https://www.pornhub.com/view_video.php?viewkey=ph623b5acbe541d"
    assert source(url) == "pornhub"
    assert "unavailable" in extractor_error(url, b"This video has been disabled")
