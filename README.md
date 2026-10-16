# MIKA NEXUS

Private, Telegram-native media utility. It asks for **✅ Yes / ❌ No** before any network work on each submitted URL. `/music`, `/search`, and `/podcast` search public catalogs; provider failures are shown without invented results.

## Current build status

The bot is deployed on the supplied Freestyle Ubuntu VM with a dedicated systemd service. All 15 tests, Ruff, and mypy pass there. Telegram `getMe` succeeded and live Apple/Deezer searches returned results. The VM is **paused** to conserve its free runtime quota; resume it before testing the bot. No owner-driven Telegram conversation, real media delivery, or other live provider acceptance has been completed. See [test report](docs/TEST_REPORT.md) and [provider matrix](docs/PROVIDER_MATRIX.md).

## Commands

`/start`, `/help`, `/search [query]`, `/music [query]`, `/podcast [query]`, `/twitter`, `/instagram`, `/tiktok`, `/youtube`, `/settings`, `/queue`, `/status`, `/cancel JOB_ID`, `/admin` (owner only). There is no `/x` command and no web interface.

## Setup in isolated Ubuntu workspace

1. Install Python 3.12+, `uv`, `ffmpeg`, and `ffprobe` in the intended deployment environment. yt-dlp is installed through the project environment. Install Deno for modern YouTube extraction if required by the deployed yt-dlp version.
2. Copy `.env.example` to `.env`; set `BOT_TOKEN`, `OWNER_TELEGRAM_ID`, `ALLOWED_TELEGRAM_IDS` (owner plus at most three others). Keep `.env` mode `0600`.
3. Run `UV_CACHE_DIR=/tmp/mika-uv-cache uv sync --locked --extra dev`, then `uv run mika-nexus doctor`, `uv run python -m unittest discover -s tests -v`, and `uv run mika-nexus run`.
4. Start in `BOT_API_MODE=public`; uploads above 48 MiB are refused. Local Bot API requires deliberate migration and separate server setup (see [installation](docs/INSTALL_UBUNTU.md)).

All public link submissions receive one separate consent prompt per URL, up to five. A No deletes only the bot prompt. A Yes creates a durable job. Ordinary text and Telegram media are ignored outside a short explicit search query session.

## Design and limitations

SQLite WAL holds consent, jobs, result snapshots, collection browsing, and Telegram file ID cache. The worker allows two concurrent jobs globally and one per user. Direct files are streamed to private job directories. yt-dlp runs only on demand. Search metadata does not cause downloads. Metadata-only catalog hits require a new link submission and consent; they are never represented as Spotify/Apple/Deezer audio bytes.

Current limitations include no automated local Bot API migration, no RSS discovery/search index, no robust disk reservation, and no verified end-to-end Telegram download. Deno is not installed on the VM, so some modern YouTube extraction paths may fail. Apple and Deezer search have been probed; other providers remain unverified. See [security](docs/SECURITY.md) and [dependency notices](docs/DEPENDENCIES.md).
