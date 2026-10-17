# MIKA NEXUS

Private, Telegram-native media utility. It asks for **✅ Yes / ❌ No** before any network work on each submitted URL. `/music`, `/search`, and `/podcast` search public catalogs; provider failures are shown without invented results.

## Current build status

The current source build passes **51 local tests**, Ruff, and mypy. Earlier Freestyle VM tests confirmed several Spotify, SoundCloud, Castbox and YouTube community-post deliveries, but that VM has reached its quota and **this source build has not been deployed or live-tested there**. The Telegram bot on that VM is offline. The full acceptance checklist is still open, especially anonymous Instagram stories/carousels and TikTok sound pages. The supplied Pornhub sample is gone (HTTP 410). See the [test report](docs/TEST_REPORT.md), [owner status](docs/OWNER_ACCEPTANCE.md), [provider matrix](docs/PROVIDER_MATRIX.md), [acceptance ledger](docs/ACCEPTANCE_V1.md), and [21-repository source audit](docs/THIRD_PARTY_SOURCE_AUDIT.md).

## Commands

`/start`, `/help`, `/search [query]`, `/music [query]`, `/podcast [query]`, `/twitter`, `/instagram`, `/tiktok`, `/youtube`, `/settings`, `/queue`, `/status`, `/cancel [JOB_ID]`, `/admin` (owner only). There is no `/x` command and no web interface.

## Setup in isolated Ubuntu workspace

1. Install Python 3.12+, `uv`, `ffmpeg`, and `ffprobe` in the intended deployment environment. yt-dlp, Deno and yt-dlp-ejs are installed through the locked project environment.
2. Copy `.env.example` to `.env`; set `BOT_TOKEN`, `OWNER_TELEGRAM_ID`, `ALLOWED_TELEGRAM_IDS` (owner plus at most three others). Optional Spotify catalog search and show browsing require `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`. Keep `.env` mode `0600`.
3. Run `UV_CACHE_DIR=/tmp/mika-uv-cache uv sync --locked --extra dev`, then `uv run mika-nexus doctor`, `uv run python -m unittest discover -s tests -v`, and `uv run mika-nexus run`.
4. Start in `BOT_API_MODE=public`; uploads above 48 MiB are refused. Local Bot API requires deliberate migration and separate server setup (see [installation](docs/INSTALL_UBUNTU.md)).

All public link submissions receive one separate consent prompt per URL, up to five. A No deletes only the bot prompt. A Yes creates a durable job. Ordinary text and Telegram media are ignored outside a short explicit search query session.

## Design and limitations

SQLite WAL holds consent, jobs, result snapshots, collection browsing, and Telegram file ID cache. The worker allows two concurrent jobs globally and one per user. Direct files are streamed to private job directories. yt-dlp runs only on demand. Search metadata does not cause downloads. Catalog hits can be selected in search or submitted as a link; they are matched to separate public recordings with strict title, artist, and duration checks. They are never represented as Spotify/Apple/Deezer audio bytes.

Current limitations include no automated local Bot API migration, no global RSS discovery/search index, no robust disk reservation or HTTP Range resume, and no reliable anonymous access to every Instagram story, photo, or profile. Some source formats require login or are no longer public. See [security](docs/SECURITY.md) and [dependency notices](docs/DEPENDENCIES.md).
