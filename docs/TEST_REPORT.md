# Test report — 2026-10-16

Executed on the supplied Freestyle Ubuntu 24.04 VM in `/opt/mika-nexus`:

- Locked dependencies include `yt-dlp==2026.8.19`, `gallery-dl==1.32.16`, `yt-dlp-ejs==0.8.0`, and `deno==2.9.7`. Ruff, mypy and **24 automated tests** pass.
- The systemd bot service was active after deployment. An earlier Telegram `getMe` check succeeded for `@mikadlbot`; owner-approved TikTok video/photo and Instagram Reel jobs had Telegram receipts.
- Public Spotify track metadata resolved to title **Never Gonna Give You Up** and artist **Rick Astley**. A public Spotify album returned 18 real tracks. The combined SoundCloud/YouTube Music search returned 35 candidates without provider errors. Some bootlegs were ranked too highly; a later patch penalized extra title words and explicit variants.
- Apple Podcasts search for `science` returned 30 podcast/episode results, and one real public RSS feed returned 30 selectable episodes.
- YouTube extraction with Deno/EJS simulated successfully. A real YouTube video audio extraction produced one `.mp3`, 7,045,581 bytes; `ffprobe` reported `codec_name=mp3` and exit status 0. A new Job finished `COMPLETED` with one actual Telegram audio receipt.
- Several previously completed TikTok short URLs now resolve to public video/photo URLs. One video failed both yt-dlp and gallery-dl CDN (403), then a public metadata/media fallback returned a 3,363,897-byte MP4 whose audio and video passed ffprobe; transcoding it produced a real MP3. The fallback checks that its post ID matches the requested URL. After bounded retry handling, the same video completed as a Telegram Job in 10 seconds with one document receipt. A previously submitted Instagram photo timed out in `gallery-dl` after 45 seconds; it remains **BLOCKED**.

Still to confirm through Telegram on this build: a complete new music candidate selection and delivery, MP3 button callback, podcast episode upload, album selection, public Instagram photo/carousel, X media, profile browser, and other platforms. Spotify official search requires client credentials. Spotify playlist public embed returned a 500 response during inspection. Anonymous Instagram story/highlight access can require a valid session. There is no local Bot API mode migration or full licensed audio extraction from Spotify, Apple Music or Deezer.
