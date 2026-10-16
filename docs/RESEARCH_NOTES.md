# Downloader research · 2026-10-16

The following repositories were cloned into temporary directories and their relevant search, download, lyrics, or queue code inspected. They are reference implementations; no third-party code was copied into this project.

| Project | Relevant finding | Decision here |
| --- | --- | --- |
| yt-dlp, gallery-dl, Instaloader, cobalt, scdl | Platform extractors and their public access limits differ; galleries and stories need source-specific handling. | Keep bounded adapters and report upstream access errors. |
| spotDL (`spotDL/spotify-downloader`) | Separates catalog metadata from public recording search and supports several lyric providers. | Preserve catalog identity, compare recording duration, try a second lyric provider when needed. |
| `GeiserX/telegram-slskd-local-bot` | Retries music lookup with a cleaned title, then title-only and reduced queries; ranks candidates. Its slskd backend requires a separate service. | Add a metadata alias search for non-Latin artists and retain strict matching. |
| `EINDRAL/all-in-one-downloader-bot` | Uses `syncedlyrics`, but its Spotify path selects the first YouTube result without checking identity. | Do not adopt first-result selection; prefer complete verified lyrics and full-duration recordings. |
| `arashnm80/spot-seek-bot` | Uses Spotify track IDs and a Telegram file cache; some other providers are explicitly unsupported. | Continue caching delivered Telegram file IDs and avoid claiming unsupported platform success. |
| `Masterolic/Spotify-Downloader` | Uses a Genius-based lyric path and a first-result music search. | Use the LRCLIB result set plus lyrics.ovh fallback; keep duration validation. |
| `tangyoha/telegram_media_downloader` | Uses a queue, retries, and timeout controls for Telegram media transfer. | Keep the existing persistent queue and bound federated search to one deadline. |
| `arshamalh/podcribe` | Focuses on podcast feeds and transcription rather than full media download. | Keep feed pagination and the transcription placeholder. |

For the owner's failing Spotify example, the catalog says artist `イイダサトミ`, while Apple Music lists the same 293-second track as `Satomi Iida`. The original YouTube Music query returned no items. Removing punctuation from a query using that public alias found full-duration public candidates. The first live attempt exposed a second punctuation mismatch; after correcting the query and alias-aware score, the live bot completed the audio job and recorded Telegram media message 443. The owner can confirm the recording by listening.
