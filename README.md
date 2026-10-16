# MIKA NEXUS

Private, Telegram-native media utility. It asks for **✅ Yes / ❌ No** before any network work on each submitted URL. `/music`, `/search`, and `/podcast` search public catalogs; provider failures are shown without invented results.

## Current build status

The bot is deployed on the supplied Freestyle Ubuntu VM with a dedicated systemd service. The current build has 24 passing automated tests, Ruff and mypy checks. A public Spotify track resolved to its title and artist, and live SoundCloud/YouTube Music search returned 35 candidates. A public Spotify album returned 18 selectable tracks. An Apple Podcasts search returned podcasts and episodes, and one public feed exposed 30 selectable episodes. YouTube extraction works with Deno/EJS; a real video produced a verified MP3 and one Telegram audio receipt. A formerly failing TikTok video was recovered as a verified MP4 through a bounded fallback; an Instagram photo still times out at the source. **The full acceptance checklist is not yet passing.** See [test report](docs/TEST_REPORT.md), [provider matrix](docs/PROVIDER_MATRIX.md), [acceptance ledger](docs/ACCEPTANCE_V1.md), and [research](docs/RESEARCH.md).

## Commands

`/start`, `/help`, `/search [query]`, `/music [query]`, `/podcast [query]`, `/twitter`, `/instagram`, `/tiktok`, `/youtube`, `/settings`, `/queue`, `/status`, `/cancel [JOB_ID]`, `/admin` (owner only). There is no `/x` command and no web interface.

## Setup in isolated Ubuntu workspace

1. Install Python 3.12+, `uv`, `ffmpeg`, and `ffprobe` in the intended deployment environment. yt-dlp, Deno and yt-dlp-ejs are installed through the locked project environment.
2. Copy `.env.example` to `.env`; set `BOT_TOKEN`, `OWNER_TELEGRAM_ID`, `ALLOWED_TELEGRAM_IDS` (owner plus at most three others). Keep `.env` mode `0600`.
3. Run `UV_CACHE_DIR=/tmp/mika-uv-cache uv sync --locked --extra dev`, then `uv run mika-nexus doctor`, `uv run python -m unittest discover -s tests -v`, and `uv run mika-nexus run`.
4. Start in `BOT_API_MODE=public`; uploads above 48 MiB are refused. Local Bot API requires deliberate migration and separate server setup (see [installation](docs/INSTALL_UBUNTU.md)).

All public link submissions receive one separate consent prompt per URL, up to five. A No deletes only the bot prompt. A Yes creates a durable job. Ordinary text and Telegram media are ignored outside a short explicit search query session.

## Design and limitations

SQLite WAL holds consent, jobs, result snapshots, collection browsing, and Telegram file ID cache. The worker allows two concurrent jobs globally and one per user. Direct files are streamed to private job directories. yt-dlp runs only on demand. Search metadata does not cause downloads. Metadata-only catalog hits require a new link submission and consent; they are never represented as Spotify/Apple/Deezer audio bytes.

Current limitations include no automated local Bot API migration, no global RSS discovery/search index, no robust disk reservation or HTTP Range resume, and no reliable anonymous access to every Instagram story, photo, or profile. Public Spotify playlists can return an unavailable embed response. Live Telegram delivery of the new MP3 flow still requires acceptance testing. See [security](docs/SECURITY.md) and [dependency notices](docs/DEPENDENCIES.md).
