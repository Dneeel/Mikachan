# Provider capability matrix — 2026-10-16

Status refers to evidence from the supplied Freestyle VM unless stated otherwise. Metadata search does not prove download delivery.

| Source | Metadata / browsing | Download route | Current evidence |
| --- | --- | --- | --- |
| Spotify | Public track page and album embed; official search requires client ID/secret | Find selectable public SoundCloud / YouTube Music recordings; no DRM audio | Track and 18-track album live PASS; playlist embed returned 500; alternative candidates live PASS; one selected SoundCloud candidate failed due no public downloadable audio; YouTube Music candidate delivery UNVERIFIED |
| SoundCloud | `scsearch` via yt-dlp; track metadata | Extract public track to MP3 | 35 combined audio candidates live PASS; one chosen candidate FAILED at source (no public full audio); other candidates UNVERIFIED |
| YouTube Music | `ytmusicapi` track/album/playlist/podcast search | yt-dlp + ffmpeg MP3 for public track | Search live PASS; YouTube video to real MP3 and ffprobe live PASS; Telegram delivery UNVERIFIED |
| Apple Music | Public iTunes track/album/artist search and lookup | Find alternative public recording; no DRM audio | Search previously live PASS; album and track lookup code UNVERIFIED live |
| Deezer | Public catalog search and track/album/playlist API | Find alternative public recording; no DRM audio | Search previously live PASS; new selection flow UNVERIFIED live |
| Apple Podcasts / RSS | Public podcast and episode search, feed URL and bounded RSS browsing | Public enclosure file | Search returned 30 hits and public feed exposed 30 episodes live PASS; Telegram delivery UNVERIFIED |
| YouTube | yt-dlp with Deno/EJS | Video, or MP3 audio extraction | Real MP3/ffprobe and one Telegram audio receipt PASS; video delivery/quality menus UNVERIFIED or NOT IMPLEMENTED |
| TikTok | Short-link redirect and post/profile routing | yt-dlp, gallery-dl, then bounded public API fallback with post-ID check | Formerly failing video delivered as MP4 with one Telegram receipt; MP3 derivative delivered with a second Telegram audio receipt |
| Instagram | Post/reel and bounded public profile metadata where available | Gallery first for photo/carousel, yt-dlp fallback | One Reel previously delivered; tested photo gallery timed out after 45s; stories often require login |
| Twitter / X | Tweet and profile routing | Gallery first for media, yt-dlp fallback | Tested owner tweet had no video; supplied X photo returned no gallery media; image/multimedia delivery UNVERIFIED |
| Reddit / Pinterest / Facebook / Google Drive | Safe routing and bounded public extraction | yt-dlp/gallery-dl or direct stream | UNVERIFIED live |
| Direct HTTPS | Validated DNS/redirect streaming | Bounded disk stream; MP3 transcoding when requested | Automated media transcode PASS; remote delivery UNVERIFIED; Range resume NOT IMPLEMENTED |
| Public MEGA | Recognized and reported unavailable | No adapter | NOT IMPLEMENTED |

Private media and protected catalog audio are outside the public access model. Provider failures are surfaced without claiming a successful download. The source label in search results identifies where metadata was found; the actual chosen public recording is selected separately.
