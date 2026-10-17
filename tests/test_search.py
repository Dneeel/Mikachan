import unittest
import asyncio
from unittest.mock import AsyncMock

from mika_nexus.search import Hit, MAX_RESULTS, Searcher


class FederationTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_versions_and_failure(self):
        s = Searcher()
        s._apple = AsyncMock(
            return_value=[
                Hit("apple", "album", "OK Computer", "Radiohead", "https://example.com/album", "a")
            ]
        )
        s._soundcloud = AsyncMock(
            return_value=[
                Hit(
                    "soundcloud",
                    "track",
                    "Creep (Slowed + Reverb)",
                    "creator",
                    "https://soundcloud.com/u/creep",
                    "slowed",
                )
            ]
        )
        s._spotify = AsyncMock(
            return_value=[
                Hit(
                    "spotify",
                    "track",
                    "Creep",
                    "Radiohead",
                    "https://open.spotify.com/track/1",
                    "official",
                )
            ]
        )
        hits, fail = await s.search("Radiohead", {"track"}, {"apple", "soundcloud", "spotify"})
        self.assertEqual({h.source for h in hits}, {"soundcloud", "spotify"})
        self.assertEqual(len(hits), 2)
        self.assertNotEqual(hits[0].title, hits[1].title)
        hits, _ = await s.search(
            "Radiohead", {"track", "album"}, {"apple", "soundcloud", "spotify"}
        )
        self.assertEqual({h.kind for h in hits}, {"track", "album"})
        hits, _ = await s.search("Radiohead", {"track"}, {"spotify"})
        self.assertEqual({h.source for h in hits}, {"spotify"})
        s._spotify = AsyncMock(side_effect=RuntimeError("401"))
        hits, fail = await s.search("Radiohead", {"track"}, {"spotify", "soundcloud"})
        self.assertEqual({h.source for h in hits}, {"soundcloud"})
        self.assertIn("spotify", fail)

    async def test_empty_and_bounded(self):
        s = Searcher()
        s._apple = AsyncMock(
            return_value=[
                Hit("apple", "track", str(i), "a", "https://example.com", str(i))
                for i in range(200)
            ]
        )
        hits, _ = await s.search("a", set(), {"apple"})
        self.assertEqual(hits, [])
        hits, _ = await s.search("a", {"track"}, {"apple"})
        self.assertEqual(len(hits), MAX_RESULTS)

    async def test_podcast_feeds_with_same_name_keep_distinct_source_ids(self):
        s = Searcher()
        s._apple_podcasts = AsyncMock(return_value=[
            Hit("apple_podcasts", "podcast", "Same Show", "Host", "https://a.example/show", "1"),
        ])
        s._spotify = AsyncMock(return_value=[
            Hit("spotify", "podcast", "Same Show", "Host", "https://b.example/show", "2"),
        ])
        hits, _ = await s.search("Same Show", {"podcast"}, {"apple_podcasts", "spotify"})
        self.assertEqual({(hit.source, hit.id) for hit in hits},
                         {("apple_podcasts", "1"), ("spotify", "2")})

    async def test_apple_podcast_types_start_without_waiting_for_each_other(self):
        from unittest.mock import MagicMock, patch

        started: set[str] = set()
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def get(_url: str, *, params: dict[str, object]) -> MagicMock:
            started.add(str(params["entity"]))
            if len(started) == 2:
                both_started.set()
            await release.wait()
            response = MagicMock()
            response.json.return_value = {"results": []}
            return response

        client = MagicMock()
        client.get = get
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch("mika_nexus.search.httpx.AsyncClient", return_value=client):
            task = asyncio.create_task(Searcher()._apple_podcasts("show", {"podcast", "episode"}))
            try:
                await asyncio.wait_for(both_started.wait(), 1)
                release.set()
                self.assertEqual(await task, [])
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_spotify_podcast_search_reuses_short_lived_token(self):
        from unittest.mock import MagicMock, patch

        auth = MagicMock()
        auth.json.return_value = {"access_token": "test-token", "expires_in": 3600}
        search = MagicMock()
        search.json.return_value = {"shows": {"items": [{
            "id": "show1", "name": "Example Show", "publisher": "Publisher",
            "external_urls": {"spotify": "https://open.spotify.com/show/show1"},
        }]}, "episodes": {"items": [{
            "id": "episode1", "name": "First Episode", "duration_ms": 120000,
            "show": {"name": "Example Show"},
            "external_urls": {"spotify": "https://open.spotify.com/episode/episode1"},
        }]}}
        client = MagicMock()
        client.post = AsyncMock(return_value=auth)
        client.get = AsyncMock(return_value=search)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with (patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}),
              patch("mika_nexus.search.httpx.AsyncClient", return_value=client)):
            finder = Searcher()
            hits = await finder._spotify("example", {"podcast", "episode"})
            await finder._spotify("example", {"podcast"})
        self.assertEqual({hit.kind for hit in hits}, {"podcast", "episode"})
        self.assertEqual(next(hit.duration for hit in hits if hit.kind == "episode"), 120)
        client.post.assert_awaited_once()

    async def test_one_failed_apple_content_type_preserves_other_results(self):
        from unittest.mock import MagicMock, patch

        async def get(_url: str, *, params: dict[str, object]) -> MagicMock:
            if params["entity"] == "album":
                raise RuntimeError("album endpoint failed")
            response = MagicMock()
            response.json.return_value = {"results": [{
                "trackName": "Song", "artistName": "Singer", "trackId": 12,
                "trackViewUrl": "https://music.apple.com/song/12",
            }]}
            return response

        client = MagicMock()
        client.get = get
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch("mika_nexus.search.httpx.AsyncClient", return_value=client):
            hits = await Searcher()._apple("Song", {"track", "album"})
        self.assertEqual([(hit.kind, hit.title) for hit in hits], [("track", "Song")])


if __name__ == "__main__":
    unittest.main()
