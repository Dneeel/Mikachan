"""User-visible media text and music links."""

import json

from mika_nexus.jobs import Worker
from mika_nexus.music_ui import listening_links


def test_public_post_caption_survives(tmp_path):
    (tmp_path / "01.info.json").write_text(
        json.dumps({"description": "متن اصلی پست & عکس‌ها", "uploader": "Creator"})
    )
    caption, overflow = Worker._caption("https://example.org/post/1", tmp_path)
    assert "متن اصلی پست &amp; عکس‌ها" in caption
    assert "Creator" in caption
    assert "https://example.org/post/1" in caption
    assert overflow == ""


def test_listening_links_mark_search_destinations():
    links = listening_links("Blud Slowed", "Artist", "soundcloud", "https://soundcloud.com/a/b")
    assert links[2] == ("SoundCloud", "https://soundcloud.com/a/b")
    assert "🔎" in links[0][0] and "search" in links[0][1]
    assert "🔎" in links[1][0] and "search" in links[1][1]
