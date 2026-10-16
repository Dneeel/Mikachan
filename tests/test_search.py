import unittest
from unittest.mock import AsyncMock

from mika_nexus.search import Hit, Searcher


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
                for i in range(100)
            ]
        )
        hits, _ = await s.search("a", set(), {"apple"})
        self.assertEqual(hits, [])
        hits, _ = await s.search("a", {"track"}, {"apple"})
        self.assertEqual(len(hits), 50)


if __name__ == "__main__":
    unittest.main()
