# MIKA NEXUS

Private, Telegram-native media utility. It asks for **✅ Yes / ❌ No** before any network work on each submitted URL. `/music`, `/search`, and `/podcast` search public catalogs; provider failures are shown without invented results.

## Current build status

A substantial source build is implemented and fourteen dependency-light unit tests pass in the cloud workspace. The full aiogram runtime, extractor integration, Telegram API, and live providers have **not** been verified here because package installation was blocked by network permission. See [test report](docs/TEST_REPORT.md) and [provider matrix](docs/PROVIDER_MATRIX.md). This is a development build, not a production-proven V1 release.

## Commands

`/start`, `/help`, `/search [query]`, `/music [query]`, `/podcast [query]`, `/twitter`, `/instagram`, `/tiktok`, `/youtube`, `/settings`, `/queue`, `/status`, `/cancel JOB_ID`, `/admin` (owner only). There is no `/x` command and no web interface.

## Setup in isolated Ubuntu workspace

1. Install Python 3.12+, `uv`, `ffmpeg`, and `ffprobe` in the intended deployment environment. yt-dlp is installed through the project environment. Install Deno for modern YouTube extraction if required by the deployed yt-dlp version.
2. Copy `.env.example` to `.env`; set `BOT_TOKEN`, `OWNER_TELEGRAM_ID`, `ALLOWED_TELEGRAM_IDS` (owner plus at most three others). Keep `.env` mode `0600`.
3. Run `UV_CACHE_DIR=/tmp/mika-uv-cache uv sync --extra dev`, then `uv run mika-nexus doctor`, `uv run python -m unittest discover -s tests -v`, and `uv run mika-nexus run`.
4. Start in `BOT_API_MODE=public`; uploads above 48 MiB are refused. Local Bot API requires deliberate migration and separate server setup (see [installation](docs/INSTALL_UBUNTU.md)).

All public link submissions receive one separate consent prompt per URL, up to five. A No deletes only the bot prompt. A Yes creates a durable job. Ordinary text and Telegram media are ignored outside a short explicit search query session.

## Design and limitations

SQLite WAL holds consent, jobs, result snapshots, collection browsing, and Telegram file ID cache. The worker allows two concurrent jobs globally and one per user. Direct files are streamed to private job directories. yt-dlp runs only on demand. Search metadata does not cause downloads. Metadata-only catalog hits require a new link submission and consent; they are never represented as Spotify/Apple/Deezer audio bytes.

Current limitations include no verified live provider probes, no automated local Bot API migration, no RSS discovery/search index, no robust disk reservation, and no verified end-to-end Telegram test. See [security](docs/SECURITY.md) and [dependency notices](docs/DEPENDENCIES.md). The code must be reviewed and acceptance-tested with the owner on a staging bot before any VPS deployment.
