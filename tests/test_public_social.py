"""Public social adapters must validate identities and sort pinned timelines."""

from unittest.mock import AsyncMock

import pytest

from mikachan.follow import command_target, public_items
from mikachan.jobs import extractor_error, gallery_media_headers
from mikachan.security import source
from mikachan.public_social import (_get, public_statuses, public_instagram_profile,
                                     public_tiktok_profile, public_x_profile,
                                     public_tiktok_sound, public_x_media)


@pytest.mark.asyncio
async def test_instagram_public_page_exposes_only_verified_name_and_stable_avatar(monkeypatch):
    import httpx

    html = ('<meta property="og:title" content="Real Name (@alice) • Instagram">'
            '<meta property="og:image" content="https://scontent.cdninstagram.com/'
            'v/t51.2885-19/123456789012345_123456789012345_n.jpg?token=rotates">')
    original = httpx.AsyncClient
    monkeypatch.setattr("mikachan.public_social.validate_dns", lambda _url: None)
    monkeypatch.setattr("mikachan.public_social.httpx.AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(
                            lambda _request: httpx.Response(200, text=html)), **kwargs))
    assert await public_instagram_profile("alice") == {
        "name": "Real Name", "avatar": "123456789012345_123456789012345_n.jpg"}
    assert (await public_instagram_profile("alice", display_avatar=True))["avatar"].startswith(
        "https://scontent.cdninstagram.com/")


@pytest.mark.asyncio
async def test_instagram_public_page_rejects_mismatched_identity(monkeypatch):
    import httpx

    original = httpx.AsyncClient
    monkeypatch.setattr("mikachan.public_social.validate_dns", lambda _url: None)
    monkeypatch.setattr("mikachan.public_social.httpx.AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(
                            lambda _request: httpx.Response(200, text=(
                                '<meta property="og:title" content="Other (@bob) • Instagram">'))),
                            **kwargs))
    with pytest.raises(ValueError, match="identity mismatch"):
        await public_instagram_profile("alice")


@pytest.mark.asyncio
async def test_transient_public_timeline_failure_gets_one_bounded_retry(monkeypatch):
    import httpx

    calls = 0
    def handle(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(404 if calls == 1 else 200,
                              json={"code": 200, "results": []})

    original = httpx.AsyncClient
    monkeypatch.setattr("mikachan.public_social.validate_dns", lambda _url: None)
    monkeypatch.setattr("mikachan.public_social.httpx.AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    assert await _get("twitter", "/2/profile/alice/statuses?count=15") == {
        "code": 200, "results": []}
    assert calls == 2


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
async def test_tiktok_profile_fallback_checks_identity_and_stable_avatar(monkeypatch):
    payload = {"code": 200, "user": {
        "screen_name": "bnetanyahu", "name": "Public Name", "description": "Public bio",
        "avatar_url": "https://p16-common-sign.tiktokcdn-eu.com/path/"
                      "32de12cc61222299960546fe18461e36~tplv-a.jpeg?expires=1"}}
    monkeypatch.setattr("mikachan.public_social._get", AsyncMock(return_value=payload))
    assert await public_tiktok_profile("bnetanyahu") == {
        "name": "Public Name", "bio": "Public bio",
        "avatar": "32de12cc61222299960546fe18461e36"}
    payload["user"]["screen_name"] = "different"
    with pytest.raises(ValueError, match="identity"):
        await public_tiktok_profile("bnetanyahu")


@pytest.mark.asyncio
async def test_x_profile_fallback_rejects_stale_cache(monkeypatch):
    import httpx
    import time
    payload = {"screen_name": "Dniuis", "name": "DNI", "description": "New bio",
               "fetched_on": time.time() - 60,
               "profile_image_url": "https://pbs.twimg.com/profile_images/1/abc_normal.jpg"}
    original = httpx.AsyncClient
    monkeypatch.setattr("mikachan.public_social.validate_dns", lambda _url: None)
    monkeypatch.setattr("mikachan.public_social.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=payload)), **kwargs))
    result = await public_x_profile("Dniuis")
    assert result["bio"] == "New bio" and result["avatar"].endswith("/abc.jpg")
    payload["fetched_on"] = time.time() - 86400
    with pytest.raises(ValueError, match="stale"):
        await public_x_profile("Dniuis")


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
