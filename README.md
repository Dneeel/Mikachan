# MIKA NEXUS

Private, Telegram-native media utility. It asks for **✅ Yes / ❌ No** before any network work on each submitted URL. `/music`, `/search`, and `/podcast` search public catalogs; provider failures are shown without invented results.

## Current build status

The bot is deployed on the supplied Freestyle Ubuntu VM with a dedicated systemd service. The current build has 30 passing automated tests, Ruff and mypy checks. The owner's Spotify sample yielded a verified full-length 220-second MP3 and a completed Telegram media receipt. A public Spotify playlist exposed 87 correctly named tracks, the owner's SoundCloud set opened a Telegram menu with three real titles, and the Jafekri RSS feed exposed 221 episodes. The owner's Castbox episode and SoundCloud track completed Telegram delivery. Public Apple and Spotify episode links can also be matched to exact public podcast enclosures. Videos and photos use Telegram's native media methods, with WebM video converted to MP4. **The full acceptance checklist is not yet passing**, especially anonymous Instagram stories/carousels, TikTok sound pages, and YouTube community image posts. The supplied Pornhub sample is gone (HTTP 410). The quota-limited VM is paused between test sessions, so the bot is offline while paused. See [test report](docs/TEST_REPORT.md), [owner status](docs/OWNER_ACCEPTANCE.md), [provider matrix](docs/PROVIDER_MATRIX.md), [acceptance ledger](docs/ACCEPTANCE_V1.md), and [research](docs/RESEARCH.md).

## Commands

`/start`, `/help`, `/search [query]`, `/music [query]`, `/podcast [query]`, `/twitter`, `/instagram`, `/tiktok`, `/youtube`, `/settings`, `/queue`, `/status`, `/cancel [JOB_ID]`, `/admin` (owner only). There is no `/x` command and no web interface.

## Setup in isolated Ubuntu workspace

1. Install Python 3.12+, `uv`, `ffmpeg`, and `ffprobe` in the intended deployment environment. yt-dlp, Deno and yt-dlp-ejs are installed through the locked project environment.
2. Copy `.env.example` to `.env`; set `BOT_TOKEN`, `OWNER_TELEGRAM_ID`, `ALLOWED_TELEGRAM_IDS` (owner plus at most three others). Keep `.env` mode `0600`.
3. Run `UV_CACHE_DIR=/tmp/mika-uv-cache uv sync --locked --extra dev`, then `uv run mika-nexus doctor`, `uv run python -m unittest discover -s tests -v`, and `uv run mika-nexus run`.
4. Start in `BOT_API_MODE=public`; uploads above 48 MiB are refused. Local Bot API requires deliberate migration and separate server setup (see [installation](docs/INSTALL_UBUNTU.md)).

All public link submissions receive one separate consent prompt per URL, up to five. A No deletes only the bot prompt. A Yes creates a durable job. Ordinary text and Telegram media are ignored outside a short explicit search query session.

## Design and limitations

SQLite WAL holds consent, jobs, result snapshots, collection browsing, and Telegram file ID cache. The worker allows two concurrent jobs globally and one per user. Direct files are streamed to private job directories. yt-dlp runs only on demand. Search metadata does not cause downloads. Catalog hits can be selected in search or submitted as a link; they are matched to separate public recordings with strict title, artist, and duration checks. They are never represented as Spotify/Apple/Deezer audio bytes.

Current limitations include no automated local Bot API migration, no global RSS discovery/search index, no robust disk reservation or HTTP Range resume, and no reliable anonymous access to every Instagram story, photo, or profile. Some source formats require login or are no longer public. See [security](docs/SECURITY.md) and [dependency notices](docs/DEPENDENCIES.md).
