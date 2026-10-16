# Provider capability matrix

Apple Music and Deezer search returned live results on the supplied VM on 2026-10-16. Other live routes remain unverified.

| Source | Catalog search implemented | Download route | Credential requirement | Status |
| --- | --- | --- | --- | --- |
| Apple Music | Track, album, artist via public iTunes API | None for licensed tracks; link may be handled by extractor only if public | None | TRACK SEARCH VERIFIED |
| Apple Podcasts | Podcast, episode via public iTunes API | Public episode link may be handled | None | UNVERIFIED |
| Deezer | Track, album, artist, playlist via public API | Metadata only; no protected audio | None | TRACK SEARCH VERIFIED |
| SoundCloud | Track via yt-dlp `scsearch` | yt-dlp public item after separate consent | None for public extractor path | SEARCH AND MEDIA PROBE VERIFIED; DELIVERY UNVERIFIED |
| YouTube Music | Track, album, artist, playlist, podcast, episode via `ytmusicapi` | yt-dlp public item after separate consent | None by default | SEARCH AND MEDIA PROBE VERIFIED; DELIVERY UNVERIFIED |
| Spotify | Track, album, artist, playlist via official search API | Metadata only; no full Spotify audio | Client ID/secret for this adapter | CREDENTIALS REQUIRED; FULL AUDIO UNAVAILABLE |
| Podcast RSS / Castbox | No global catalog search | Public URL via extractor where supported | None | PARTIAL, UNVERIFIED |
| YouTube, TikTok, Instagram, Twitter/X, Reddit, Pinterest | No dedicated catalog search | yt-dlp public item; gallery-dl fallback for TikTok photos | Public extraction only | TikTok video/photo and one Instagram Reel DELIVERY VERIFIED; Instagram stories require login; tested X post contained no video; other routes UNVERIFIED |
| Direct HTTPS | No search | Bounded stream download | None | UNVERIFIED live |

Search failures are reported in the UI. Sources can be independently selected; unavailable sources are not filled with another source's results. Apple previews are not treated as full songs. yt-dlp provider behavior can change with upstream sites and may require authentication for some content; this app does not acquire personal sessions.
