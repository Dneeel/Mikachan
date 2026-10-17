"""Regression tests for lyric completeness and non-Latin track aliases."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from mika_nexus.catalog import CatalogTrack, recording_search_alias
from mika_nexus.music_ui import _genius_lyrics, verified_lyrics


def response(payload: object) -> MagicMock:
    item = MagicMock()
    item.json.return_value = payload
    return item


class MusicLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefers_complete_lyrics_for_matching_duration(self):
        partial = {"trackName": "Soldier Side", "artistName": "System Of A Down",
                   "duration": 54, "plainLyrics": "one\ntwo\nthree\nfour"}
        full = {"trackName": "Soldier Side", "artistName": "System Of A Down",
                "duration": 220, "plainLyrics": "\n".join(f"line {n}" for n in range(35))}
        client = MagicMock()
        client.get = AsyncMock(return_value=response([partial, full]))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch("mika_nexus.music_ui.httpx.AsyncClient", return_value=client):
            lyric = await verified_lyrics("Soldier Side", "System Of A Down", 220)
        self.assertEqual(len(lyric.splitlines()), 35)
        client.get.assert_awaited_once()

    async def test_non_latin_artist_uses_matching_public_alias(self):
        track = CatalogTrack("THANATOS -IF CAN'T BE YOURS- (劇場版)", "イイダサトミ", "url", 293)
        client = MagicMock()
        client.get = AsyncMock(return_value=response({"results": [
            {"trackName": "Thanatos - If I Can't Be Yours", "artistName": "Satomi Iida",
             "trackTimeMillis": 293466},
        ]}))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch("mika_nexus.catalog.httpx.AsyncClient", return_value=client):
            alias = await recording_search_alias(track)
        self.assertEqual(alias[1], "Satomi Iida")

    async def test_non_latin_title_uses_artist_and_duration_to_find_alias(self):
        track = CatalogTrack("ドレス", "BUCK-TICK", "url", 381)
        client = MagicMock()
        client.get = AsyncMock(return_value=response({"results": [
            {"trackName": "Dress", "artistName": "BUCK-TICK", "trackTimeMillis": 381000},
        ]}))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch("mika_nexus.catalog.httpx.AsyncClient", return_value=client):
            alias = await recording_search_alias(track)
        self.assertEqual(alias, ("Dress", "BUCK-TICK"))

    async def test_ambiguous_same_artist_and_duration_is_not_an_alias(self):
        track = CatalogTrack("魔法の鏡", "Yumi Arai", "url", 194)
        client = MagicMock()
        client.get = AsyncMock(return_value=response({"results": [
            {"trackName": "Mirror", "artistName": "Yumi Arai", "trackTimeMillis": 194000},
            {"trackName": "Magic", "artistName": "Yumi Arai", "trackTimeMillis": 194500},
        ]}))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch("mika_nexus.catalog.httpx.AsyncClient", return_value=client):
            alias = await recording_search_alias(track)
        self.assertIsNone(alias)

    async def test_genius_uses_attributed_full_page(self):
        api = response({"response": {"sections": [{"hits": [{"result": {
            "title": "ドレス (Dress)", "primary_artist": {"name": "BUCK-TICK"},
            "url": "https://genius.com/Buck-tick-dress-lyrics",
        }}]}]}})
        page = MagicMock()
        page.text = ('<div data-lyrics-container="true">'
                     + '<br>'.join(f'line {n}' for n in range(12)) + '</div>')
        client = MagicMock()
        client.get = AsyncMock(side_effect=[api, page])
        lyric = await _genius_lyrics(client, "ドレス", "BUCK-TICK")
        self.assertEqual(len(lyric.splitlines()), 12)


if __name__ == "__main__":
    unittest.main()
