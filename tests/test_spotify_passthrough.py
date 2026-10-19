import json

import httpx
import pytest

from mikachan.podcasts import _PublisherAudioUnavailable, _spotify_passthrough


@pytest.mark.asyncio
async def test_spotify_publisher_passthrough_requires_alive_audio(monkeypatch):
    monkeypatch.setattr("mikachan.podcasts.validate_dns", lambda _url: None)
    audio_url = "https://publisher.example/episode.mp3"
    data = {"props": {"pageProps": {"state": {"data": {
        "defaultAudioFileObject": {"passthrough": "ALLOWED", "passthroughUrl": audio_url,
                                   "url": ["https://protected.example/file"]},
        "entity": {"title": "Real episode", "subtitle": "Publisher", "duration": 726000},
    }}}}}
    page = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
    alive = True

    def respond(request):
        if request.url.host == "open.spotify.com":
            return httpx.Response(200, text=page)
        if alive:
            return httpx.Response(206, headers={"content-type": "audio/mpeg"}, content=b"ID3")
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await _spotify_passthrough(
            client, "2oFIpvUeKFv8zJ2aq70fOx",
            "https://open.spotify.com/episode/2oFIpvUeKFv8zJ2aq70fOx")
        assert result and result["url"] == audio_url and result["duration"] == 726
        alive = False
        with pytest.raises(_PublisherAudioUnavailable):
            await _spotify_passthrough(client, "2oFIpvUeKFv8zJ2aq70fOx", "source")
