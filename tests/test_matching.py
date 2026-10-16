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
            patch("mika_nexus.catalog.validate_dns"),
            patch("mika_nexus.catalog.httpx.AsyncClient", return_value=client),
        ):
            items, title = await resolve_collection("https://open.spotify.com/album/abc")
        self.assertEqual(title, "Album")
        self.assertEqual(len(items), 50)
        self.assertEqual(items[6]["url"], "https://open.spotify.com/track/6")
