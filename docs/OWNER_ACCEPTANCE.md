# Owner acceptance status · 2026-10-16

The bot asks for confirmation before processing a submitted link. A source
must expose public media to this server. The checks below distinguish code
support from confirmed delivery in Telegram.

| Request | Current status |
| --- | --- |
| Native photo/video, MP4 conversion, compact captions, Link/MP3 buttons | Implemented; synthetic media tests pass; live Telegram acceptance pending |
| TikTok photo carousel soundtrack | Public fallback now attaches soundtrack as MP3 when the post API exposes it; live acceptance pending |
| Profile card, avatar file, latest post, stories | Card and actions implemented; access varies by source; Instagram/TikTok public profile acceptance pending |
| YouTube/Spotify/SoundCloud collection browsing, quality choice, ordered download all | Implemented; owner's SoundCloud set created a Telegram menu with three real track titles; Spotify 87-track collection probed; ordered full playlist delivery pending |
| Spotify track identity, full duration, title/artist/cover | Owner's sample yielded a verified 220.296-second MP3 and one completed Telegram media receipt; cover visibility in the client remains unchecked |
| Apple/Spotify/Castbox podcast episode links | Exact public enclosure matching implemented; owner's Castbox sample completed Telegram delivery with one receipt and correct metadata; owner's Spotify and Apple samples resolved in metadata checks |
| Podcast search ranking and full feed pagination | Implemented; Jafekri feed exposes 221 episodes; Castbox search is not implemented |
| Podcast source Link, transcript placeholder, oversized audio compression | Implemented; large Telegram upload acceptance pending |
| Instagram story/highlight/carousel and sound pages | Anonymous source access unreliable; supplied story explicitly requires login; no successful acceptance |
| TikTok music pages | Upstream extractor does not currently work for the tested music URL; no successful acceptance |
| YouTube community image posts | Both owner samples completed Telegram delivery as native photo groups, with nine and three image receipts; a distinct Shorts slideshow remains unverified |
| Pornhub sample | Source returns HTTP 410 Gone; a live public URL is needed to test platform support |

Spotify, Apple Music, and Deezer catalog pages do not supply downloadable full
audio. The bot searches for a separate public recording and rejects weak or
short matches; an exact match may be unavailable for some tracks. Private
accounts are outside scope as requested.
