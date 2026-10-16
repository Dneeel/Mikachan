# Source research, 2026-10-16

Six public projects were cloned into `/tmp/mika-research` and their relevant implementations inspected. The bot uses its own integration code and pinned dependencies; no implementation was copied into this repository.

| Project | Inspected revision | Relevant implementation | Finding used here |
| --- | --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | `8973412` | `yt_dlp/YoutubeDL.py`, README JavaScript runtime and EJS guidance | Keep extraction in a bounded child process, use ffmpeg for audio postprocessing, install Deno and EJS for YouTube. |
| [gallery-dl](https://github.com/mikf/gallery-dl) | `ef30ea9` | `gallery_dl/extractor/instagram.py`, `twitter.py`, `tiktok.py`, `job.py` | Instagram sidecars and X images are separate ordered media. Use a bounded gallery path for such posts; metadata lists can support public profile browsing. |
| [spotDL](https://github.com/spotDL/spotify-downloader) | `cd4a420` | `spotdl/types/song.py`, `providers/audio/base.py`, `ytmusic.py`, `soundcloud.py` | Separate catalog identity from recording candidates. Compare title, artist, variant, and available duration; let the user choose uncertain matches. |
| [scdl](https://github.com/flyingrub/scdl) | `061c436` | `scdl/scdl.py` | SoundCloud tracks and playlists are distinct; original file availability varies. Keep the public extractor route separate from catalog search. |
| [Instaloader](https://github.com/instaloader/instaloader) | `7efa2de` | `instaloader/instaloader.py`, `structures.py` | Public post and profile extraction can work anonymously, while stories and highlights often raise explicit login requirements. |
| [cobalt](https://github.com/imputnet/cobalt) | `a636575` | `api/src/processing/service-patterns.js`, `match.js`, `services/` | Route by validated service and media type. A generic downloader should report unsupported or restricted sources clearly. |

The dependency itself can change behavior as source sites change. Public catalog pages do not provide Spotify, Apple Music, or Deezer licensed full audio. This project resolves a catalog item, searches public recordings, and asks the user to select a candidate. It does not claim those files came from the catalog service. Matching scores prioritize title, artist, version, and duration when available; ISRC is retained when exposed by catalog APIs but is rarely available in public recording metadata. No candidate is fabricated to fill a results page.

`gallery-dl` may require a valid user session for some otherwise public Instagram stories, highlights, and profiles. No session cookies were supplied. This remains a source access constraint, not evidence that those downloads work anonymously.
