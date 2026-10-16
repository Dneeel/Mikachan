# Test report — 2026-10-16

Executed on the supplied Freestyle Ubuntu 24.04 VM in `/opt/mika-nexus`:

- `uv sync --extra dev` installed locked dependencies, including `yt-dlp==2026.8.19` and `gallery-dl==1.32.16`.
- Ruff lint and formatting checks passed; mypy reported no issues in 10 source files.
- `.venv/bin/python -m unittest discover -s tests -v` — **18 tests passed**, including stale and expired Telegram callbacks, safe source error summaries, and search result confirmation.
- The service reported `ActiveState=active`, `SubState=running` after restart; Telegram `getMe` succeeded for `@mikadlbot`.
- Live Apple and Deezer catalog search returned results. The owner's stored search sessions contained results from Apple, Deezer, SoundCloud, YouTube Music, and Apple Podcasts. An existing Telegram search message was successfully edited to show a public SoundCloud result with a request button.
- One actual SoundCloud result and one YouTube Music result passed yt-dlp media probes; neither has had a Telegram delivery acceptance test.
- Owner-approved TikTok video/photo jobs completed, including a two-image photo post. An Instagram Reel that failed under the older extractor completed with one Telegram document receipt after the yt-dlp update.
- One Instagram story required login, one Instagram photo post had no available video format, one tested X post had no video, and a Spotify track did not expose downloadable full audio. Another TikTok photo post may require cookies.
- The VM was paused after checks to conserve its free runtime quota.

Full music tracks from Spotify, Apple Music, and Deezer are unavailable from public catalog pages. The Spotify search adapter also requires client credentials that have not been supplied. No personal cookies were requested or used. Deno and local Bot API mode are not configured. Other download providers have not had live delivery tests.
