# Owner acceptance status · 2026-10-16

The bot asks for confirmation before processing a submitted link. A source
must expose public media to this server. The checks below distinguish code
support from confirmed delivery in Telegram.

| Request | Current status |
| --- | --- |
| Native photo/video, MP4 conversion, compact captions, Link/MP3 buttons | Implemented; synthetic media tests pass; live Telegram acceptance pending |
| TikTok photo carousel soundtrack | Public fallback now attaches soundtrack as MP3 when the post API exposes it; live acceptance pending |
| Profile card, avatar file, latest post, stories | Card and actions implemented; access varies by source; Instagram/TikTok public profile acceptance pending |
| YouTube/Spotify/SoundCloud collection browsing, quality choice, ordered download all | Implemented; SoundCloud real metadata and Spotify 87-track collection probed; full playlist delivery pending |
| Spotify track identity, full duration, title/artist/cover | Strict match implemented; public full-length candidate found for owner's sample; delivery pending |
| Apple/Spotify podcast episode links | Exact public enclosure matching implemented; owner's Spotify episode and an Apple episode resolved in metadata checks; delivery pending |
| Podcast search ranking and full feed pagination | Implemented; Jafekri feed exposes 221 episodes; Castbox search is not implemented |
| Podcast source Link, transcript placeholder, oversized audio compression | Implemented; large Telegram upload acceptance pending |
| Instagram story/highlight/carousel and sound pages | Anonymous source access unreliable; supplied story explicitly requires login; no successful acceptance |
| TikTok music pages | Upstream extractor does not currently work for the tested music URL; no successful acceptance |
| YouTube Shorts/community image posts | No successful acceptance for the owner's image-post samples |
| Pornhub sample | Source returns HTTP 410 Gone; a live public URL is needed to test platform support |

Spotify, Apple Music, and Deezer catalog pages do not supply downloadable full
audio. The bot searches for a separate public recording and rejects weak or
short matches; an exact match may be unavailable for some tracks. Private
accounts are outside scope as requested.
