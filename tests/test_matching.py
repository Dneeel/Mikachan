import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from mika_nexus.catalog import CatalogTrack, resolve_collection, resolve_track
from mika_nexus.matching import match_score, version
from mika_nexus.store import Store


class MatchingTests(unittest.TestCase):
    def test_variants_and_artist_are_distinct(self):
        self.assertEqual(version("Creep (Slowed + Reverb)"), frozenset({"slowed", "reverb"}))
        original = match_score("Creep", "Radiohead", "Creep", "Radiohead", 239, 240)
        slowed = match_score("Creep", "Radiohead", "Creep - Slowed", "Radiohead", 239, 280)
        unrelated = match_score("Creep", "Radiohead", "Other Song", "Radiohead")
        self.assertGreater(original, slowed)
        self.assertEqual(unrelated, 0)

    def test_exact_title_cannot_mask_wrong_artist_or_duration(self):
        self.assertEqual(match_score("Creep", "Radiohead", "Creep", "Other Singer", 239, 240), 0)
        self.assertEqual(match_score("Creep", "Radiohead", "Creep", "Radiohead", 239, 280), 0)
        self.assertGreater(match_score("Creep", "Radiohead", "Radiohead - Creep (Official Audio)",
                                       "Radiohead - Topic", 239, 240), 0.84)

    def test_unofficial_and_slow_variants_remain_separate(self):
        self.assertEqual(version("Creep (Slow)"), frozenset({"slowed"}))
        self.assertEqual(version("Creep (Unofficial Tribute)"),
                         frozenset({"unofficial", "tribute"}))
        self.assertLess(match_score("Creep", "Radiohead", "Creep Unofficial Tribute",
                                    "Radiohead", 239, 240), 0.84)

    def test_derivative_job_is_owned_and_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            db = Store(Path(d) / "db.sqlite")
            parent = db.create_job(1, 1, "https://example.com/video.mp4")
            self.assertEqual(db.create_derivative(parent, 2, 1, "audio"), (None, False))
            self.assertEqual(db.create_derivative(parent, 1, 1, "audio"), (None, False))
            db.transition(parent, "QUEUED", "COMPLETED")
            child, created = db.create_derivative(parent, 1, 1, "audio")
            self.assertTrue(created)
            self.assertEqual(db.job_mode(child), "audio")
            self.assertEqual(db.create_derivative(parent, 1, 1, "audio"), (child, False))


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_spotify_playlist_pages_and_preserves_order(self):
        def response(payload):
            value = Mock()
            value.json.return_value = payload
            return value

        tracks = [{"track": {
            "id": str(i), "type": "track", "name": f"Track {i}",
            "artists": [{"name": "Artist"}], "duration_ms": 200000,
            "external_urls": {"spotify": f"https://open.spotify.com/track/{i}"},
        }} for i in range(121)]

        async def get(url, **kwargs):
            if url.endswith("/tracks"):
                offset = kwargs["params"]["offset"]
                return response({"items": tracks[offset:offset + 100]})
            return response({
                "name": "Playlist", "tracks": {"total": 121, "items": tracks[:100]},
                "owner": {"external_urls": {"spotify": "https://open.spotify.com/user/owner"}},
            })

        client = AsyncMock()
        client.get.side_effect = get
        client.post.return_value = response({"access_token": "test"})
        client.__aenter__.return_value = client
        with (
            patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}),
            patch("mika_nexus.catalog.validate_dns"),
            patch("mika_nexus.catalog.httpx.AsyncClient", return_value=client),
        ):
            items, title = await resolve_collection("https://open.spotify.com/playlist/abc")
        self.assertEqual(title, "Playlist")
        self.assertEqual(len(items), 121)
        self.assertEqual(items[100]["title"], "Track 100")
        self.assertEqual(items[0]["artist"], "Artist")
        self.assertEqual(items[0]["owner_url"], "https://open.spotify.com/user/owner")

    async def test_spotify_show_pages_all_public_episode_metadata(self):
        def response(payload):
            value = Mock()
            value.json.return_value = payload
            return value

        episodes = [{
            "id": str(i), "name": f"Episode {i}", "duration_ms": 60000,
            "external_urls": {"spotify": f"https://open.spotify.com/episode/{i}"},
        } for i in range(121)]

        async def get(url, **kwargs):
            if url.endswith("/episodes"):
                offset = kwargs["params"]["offset"]
                return response({"total": len(episodes), "items": episodes[offset:offset + 50]})
            return response({"name": "Example Podcast", "images": [{"url": "https://example.com/a.jpg"}]})

        client = AsyncMock()
        client.get.side_effect = get
        client.post.return_value = response({"access_token": "test"})
        client.__aenter__.return_value = client
        with (
            patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}),
            patch("mika_nexus.catalog.validate_dns"),
            patch("mika_nexus.catalog.httpx.AsyncClient", return_value=client),
        ):
            items, title = await resolve_collection("https://open.spotify.com/show/abc")
        self.assertEqual(title, "Example Podcast")
        self.assertEqual(len(items), 121)
        self.assertEqual(items[0]["title"], "Episode 0")
        self.assertEqual(items[-1]["url"], "https://open.spotify.com/episode/120")
        self.assertEqual(client.get.await_count, 4)

    async def test_spotify_track_metadata(self):
        response = Mock()
        response.text = (
            '<meta property="og:title" content="Song &amp; Name">'
            '<meta property="og:description" content="Artist · Album · Song">'
        )
        client = AsyncMock()
        client.get.return_value = response
        client.__aenter__.return_value = client
        with (
            patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "", "SPOTIFY_CLIENT_SECRET": ""}),
            patch("mika_nexus.catalog.validate_dns"),
            patch("mika_nexus.catalog.httpx.AsyncClient", return_value=client),
        ):
            track = await resolve_track("https://open.spotify.com/track/abc")
        self.assertEqual(
            track, CatalogTrack("Song & Name", "Artist", "https://open.spotify.com/track/abc")
        )

    async def test_spotify_album_list_is_bounded(self):
        import json

        entity = {
            "title": "Album",
            "trackList": [{"title": str(i), "uri": f"spotify:track:{i}"} for i in range(50)],
        }
        page = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
        response = Mock()
        response.text = (
            '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(page) + "</script>"
        )
        client = AsyncMock()
        client.get.return_value = response
        client.__aenter__.return_value = client
        with (
            patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "", "SPOTIFY_CLIENT_SECRET": ""}),
            patch("mika_nexus.catalog.validate_dns"),
            patch("mika_nexus.catalog.httpx.AsyncClient", return_value=client),
        ):
            items, title = await resolve_collection("https://open.spotify.com/album/abc")
        self.assertEqual(title, "Album")
        self.assertEqual(len(items), 50)
        self.assertEqual(items[6]["url"], "https://open.spotify.com/track/6")
