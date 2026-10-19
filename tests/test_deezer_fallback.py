import httpx
import pytest

from mikachan.catalog import resolve_track


@pytest.mark.asyncio
async def test_deezer_uses_curated_musicbrainz_relation_when_api_is_blocked(monkeypatch):
    identifier = "3135556"
    recording = "f1a6a40f-78f5-4918-968d-f64363bae94c"
    original_client = httpx.AsyncClient

    def respond(request):
        if request.url.host == "api.deezer.com":
            return httpx.Response(403)
        if request.url.path == "/ws/2/url":
            return httpx.Response(200, json={"relations": [
                {"target-type": "recording", "recording": {"id": recording}}]})
        if request.url.path == f"/ws/2/recording/{recording}":
            return httpx.Response(200, json={
                "title": "Harder, Better, Faster, Stronger", "length": 224293,
                "artist-credit": [{"name": "Daft Punk"}], "isrcs": ["GBDUW0000059"]})
        raise AssertionError(str(request.url))

    def client(*args, **kwargs):
        return original_client(*args, transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr("mikachan.catalog.httpx.AsyncClient", client)
    track = await resolve_track(f"https://www.deezer.com/track/{identifier}")
    assert track and track.artist == "Daft Punk" and track.duration == 224
    assert track.isrc == "GBDUW0000059"
