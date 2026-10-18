"""Exact episode resolution must not substitute an unrelated enclosure."""

import unittest
from unittest.mock import patch
from urllib.parse import quote

import httpx

from mikachan.podcasts import resolve_episode_link


class CastboxEpisodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_link_resolves_only_unique_exact_episode(self):
        original_client = httpx.AsyncClient
        title = "اپیزود دویست و هشتاد و سوم: اقتصاد انقلاب می‌کند؟"
        canonical = (
            "https://castbox.fm/episode/" + quote(title.replace(" ", "-"))
            + "-id123-id1012217789"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/ep/1012217789":
                return httpx.Response(302, headers={"location": canonical})
            if request.url.host == "castbox.fm":
                return httpx.Response(200, text='<meta property="og:title" content="Castbox">')
            return httpx.Response(200, json={"results": [
                {"trackName": title, "collectionName": "Test Podcast",
                 "episodeUrl": "https://example.org/exact.mp3", "trackTimeMillis": 4140000},
            ]})

        transport = httpx.MockTransport(handler)
        with (
            patch("mikachan.podcasts.validate_dns"),
            patch("mikachan.podcasts.httpx.AsyncClient",
                  side_effect=lambda **kw: original_client(transport=transport, **kw)),
        ):
            result = await resolve_episode_link("https://castbox.fm/vi/1012217789")
        assert result is not None
        self.assertEqual(result["title"], title)
        self.assertEqual(result["url"], "https://example.org/exact.mp3")
        self.assertEqual(result["duration"], 4140)


if __name__ == "__main__":
    unittest.main()
