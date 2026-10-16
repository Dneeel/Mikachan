"""The pinned community-post extractor must register in the runtime downloader."""

import yt_dlp


def test_youtube_post_extractor_is_registered():
    with yt_dlp.YoutubeDL({"quiet": True}) as downloader:
        assert any(
            extractor.IE_NAME == "youtube:post"
            for extractor in downloader._ies.values()
        )
