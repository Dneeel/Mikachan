# Test report — 2026-10-16

## Latest user report investigation

The 23-item owner report prompted changes to native Telegram delivery, metadata,
collection browsing, direct episode matching, and search ranking. Local Ruff,
mypy, and 27 automated tests passed before deployment. A synthetic VP9 WebM
converted to H.264 MP4 and passed ffprobe. These tests do not establish that
every third-party source is anonymously accessible.

Read-only checks on the supplied Freestyle VM found:

- The owner's SoundCloud short playlist resolved to three public tracks. A flat
  yt-dlp listing omitted all three titles; full metadata extraction returned
  their actual titles, uploaders, URLs, and durations. The browser now requests
  full SoundCloud metadata, resolves short URLs, and recognizes `/sets/` as a
  collection.
- The sample Spotify track resolves as **Soldier Side — System Of A Down**,
  duration 220 seconds, with cover art. YouTube Music returned a matching public
  221-second recording. The deployed strict matcher downloaded a 7,867,053-byte
  MP3; ffprobe measured 220.296 seconds. Telegram delivery requires a separate
  acceptance check.
- The sample Spotify playlist embed exposed 87 tracks with titles and artists.
  The Jafekri public RSS feed exposed 221 episodes. The bot now browses up to
  300 items with ten-per-page controls.
- The supplied Instagram story requires login according to yt-dlp. The supplied
  Instagram carousel also failed anonymous yt-dlp extraction. Neither is
  claimed fixed.
- The supplied Pornhub URL returns HTTP 410 Gone at source. It cannot verify a
  working adapter; a live public sample is required for that claim.
- A TikTok music-page extractor is currently unsupported upstream; music from
  photo posts is handled separately when the public post API exposes it.

The VM should be paused after final deployment to preserve the owner's free
quota. A paused VM means the Telegram bot is offline until resumed.

## Previous baseline

Executed on the supplied Freestyle Ubuntu 24.04 VM in `/opt/mika-nexus`:

- Locked dependencies include `yt-dlp==2026.8.19`, `gallery-dl==1.32.16`, `yt-dlp-ejs==0.8.0`, and `deno==2.9.7`. Ruff, mypy and **26 automated tests** pass.
- The systemd bot service was active after deployment. An earlier Telegram `getMe` check succeeded for `@mikadlbot`; owner-approved TikTok video/photo and Instagram Reel jobs had Telegram receipts.
- Public Spotify track metadata resolved to title **Never Gonna Give You Up** and artist **Rick Astley**. A public Spotify album returned 18 real tracks. The combined SoundCloud/YouTube Music search returned 35 candidates without provider errors. Some bootlegs were ranked too highly; a later patch penalized extra title words and explicit variants.
- Apple Podcasts search for `science` returned 30 podcast/episode results, and one real public RSS feed returned 30 selectable episodes.
- YouTube extraction with Deno/EJS simulated successfully. A real YouTube video audio extraction produced one `.mp3`, 7,045,581 bytes; `ffprobe` reported `codec_name=mp3` and exit status 0. A new Job finished `COMPLETED` with one actual Telegram audio receipt.
- Several previously completed TikTok short URLs now resolve to public video/photo URLs. One video failed both yt-dlp and gallery-dl CDN (403), then a public metadata/media fallback returned a 3,363,897-byte MP4 whose audio and video passed ffprobe. The fallback checks that its post ID matches the requested URL. After bounded retry handling, the same video completed as a Telegram Job in 10 seconds with one document receipt; its separate MP3 derivative completed in 9 seconds with one Telegram audio receipt. A previously submitted Instagram photo timed out in `gallery-dl` after 45 seconds; it remains **BLOCKED**.

Still to confirm through Telegram on this build: a complete new music candidate selection and delivery, the revised inline MP3 button callback, podcast episode upload, album selection, public Instagram photo/carousel, X media, profile browser, and other platforms. One selected SoundCloud candidate failed because its source did not expose public downloadable audio; no false success was reported. Spotify official search requires client credentials. Spotify playlist public embed returned a 500 response during inspection. Anonymous Instagram story/highlight access can require a valid session. There is no local Bot API mode migration or full licensed audio extraction from Spotify, Apple Music or Deezer.

Latest presentation checks: 26 tests, Ruff and mypy passed locally and on the VM. LRCLIB returned a verified lyric match for Rick Astley; a supplied TikTok video yielded public media and its real caption metadata. The supplied X photo URL produced no gallery media in an anonymous probe. Media captions and cache captions, music listening links, lyric lookup, and removal of transient progress messages were implemented; live Telegram acceptance of this final UI is still pending.
