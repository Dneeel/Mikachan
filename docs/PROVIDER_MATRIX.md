# Provider capability matrix

All live status entries below are **UNVERIFIED** in this workspace. No provider success is claimed without a network acceptance test.

| Source | Catalog search implemented | Download route | Credential requirement | Status |
| --- | --- | --- | --- | --- |
| Apple Music | Track, album, artist via public iTunes API | None for licensed tracks; link may be handled by extractor only if public | None | UNVERIFIED |
| Apple Podcasts | Podcast, episode via public iTunes API | Public episode link may be handled | None | UNVERIFIED |
| Deezer | Track, album, artist, playlist via public API | Metadata only; no protected audio | None | UNVERIFIED |
| SoundCloud | Track via yt-dlp `scsearch` | yt-dlp public item | None for public extractor path | UNVERIFIED |
| YouTube Music | Track, album, artist, playlist, podcast, episode via `ytmusicapi` | yt-dlp public item | None by default | UNVERIFIED |
| Spotify | Track, album, artist, playlist via official search API | Metadata only; never Spotify audio | Optional client ID/secret for this adapter | CREDENTIALS REQUIRED by default |
| Podcast RSS / Castbox | No global catalog search | Public URL via extractor where supported | None | PARTIAL, UNVERIFIED |
| YouTube, TikTok, Instagram, Twitter/X, Reddit, Pinterest | No dedicated catalog search | yt-dlp public item | Public extraction only | UNVERIFIED |
| Direct HTTPS | No search | Bounded stream download | None | UNVERIFIED live |

Search failures are reported in the UI. Sources can be independently selected; unavailable sources are not filled with another source's results. Apple previews are not treated as full songs. yt-dlp provider behavior can change with upstream sites and may require authentication for some content; this app does not acquire personal sessions.
