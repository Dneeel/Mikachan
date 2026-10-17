# Mikachan

Mikachan is a private, Telegram-first bot in development. Downloader, Community and Utilities share one aiogram dispatcher, one SQLite database and one bot identity. The existing downloader keeps its `mika_nexus` package and runtime names for compatibility.

The downloader asks for **✅ Yes / ❌ No** before processing each submitted URL. `/music`, `/search`, and `/podcast` search public catalogs; provider failures are shown without invented results. See the [existing system audit](docs/EXISTING_SYSTEM_AUDIT.md) and [new feature ledger](docs/COMMUNITY_UTILITIES_STATUS.md).

## Current build status

The integrated build passes **63 local tests**, Ruff, and mypy. Read-only provider probes succeeded for weather, fiat and crypto rates; NASA's current-day APOD failed but the prior day's entry loaded. No new feature was accepted through live Telegram, and this source build was not deployed. Earlier Freestyle VM downloader receipts belong to prior revisions. See the [new feature ledger](docs/COMMUNITY_UTILITIES_STATUS.md), [downloader test report](docs/TEST_REPORT.md), and [owner status](docs/OWNER_ACCEPTANCE.md).

## Commands

Downloader: `/help`, `/search [query]`, `/music [query]`, `/podcast [query]`, `/twitter`, `/instagram`, `/tiktok`, `/youtube`, `/settings`, `/queue`, `/status`, `/cancel [JOB_ID]`, `/admin` (owner only). There is no `/x` command or general `/start` dashboard.

Community: `/fal` or exact `فال`, `/poem` or exact `شعر`, `/joke`, `/quote`, `/dice`; in groups `/rules`, `/note NAME`, `/groupstats`, `/groupsettings`. Group admins/owner can use `/setrules TEXT`, `/setnote NAME TEXT`, `/delnote NAME`, `/setcmd NAME TEXT`, `/delcmd NAME`, `/welcome on|off|TEXT`, `/nickname NAME` (reply to the member, or omit to remove), `/del`, `/mute` (reply to the target message), `/modlog`, and `/groupsettings culture|notes|utilities on|off`. The cultural catalog starts empty until reviewed content is imported.

Utilities: `/qr TEXT` (or reply to text), `/scan`, `/ocr`, `/image png|jpeg|webp`, `/sticker`, `/stickerimage`, `/gif`, `/pack create SHORTNAME|TITLE|EMOJI`, `/pack add SHORTNAME|EMOJI`, `/pack remove`, `/convert json|csv`, `/weather CITY[, COUNTRY]`, `/rate USD EUR` or `/rate BTC USD`, `/time Asia/Tehran`, `/time add|remove|list`, `/apod`, `/translate en|fa TEXT`. File tools require a reply to selected media. `/admin provider fiat|crypto NAME` sets a fixed price provider. `/groupsettings`, commands, callbacks and exact triggers still require both user and group authorization.

## Setup in isolated Ubuntu workspace

1. Install Python 3.12+, `uv`, `ffmpeg`, `ffprobe`, and Tesseract in the intended deployment environment. Install `fas` Tesseract language data to enable Persian OCR. yt-dlp, Deno and yt-dlp-ejs are installed through the locked project environment.
2. Copy `.env.example` to `.env`; set `BOT_TOKEN`, `OWNER_TELEGRAM_ID`, `ALLOWED_TELEGRAM_IDS` (owner plus at most three others). To enable groups, set `PRIVATE_CHATS_ONLY=false` and seed `ALLOWED_GROUP_IDS` with negative numeric group IDs; every user still needs individual allowlisting. Optional Spotify credentials, NASA key, Mommy IDs and translation provider are described in the [feature ledger](docs/COMMUNITY_UTILITIES_STATUS.md). Keep `.env` mode `0600`.
3. Run `UV_CACHE_DIR=/tmp/mika-uv-cache uv sync --locked --extra dev`, then `uv run mika-nexus doctor`, `uv run pytest -q`, and `uv run mika-nexus run`. Import owner-reviewed content with `uv run mika-nexus import-content PATH` only after verifying its source and rights.
4. Start in `BOT_API_MODE=public`; uploads above 48 MiB are refused. Local Bot API requires deliberate migration and separate server setup (see [installation](docs/INSTALL_UBUNTU.md)).

All public link submissions receive one separate consent prompt per URL, up to five. A No deletes only the bot prompt. A Yes creates a durable job. Ordinary text and Telegram media are ignored outside an exact approved trigger or user-bound interaction.

## Design and limitations

SQLite WAL holds consent, jobs, result snapshots, collection browsing, and Telegram file ID cache. The worker allows two concurrent jobs globally and one per user. Direct files are streamed to private job directories. yt-dlp runs only on demand. Search metadata does not cause downloads. Catalog hits can be selected in search or submitted as a link; they are matched to separate public recordings with strict title, artist, and duration checks. They are never represented as Spotify/Apple/Deezer audio bytes.

Current limitations include no automated local Bot API migration, no global RSS discovery/search index, no robust disk reservation or HTTP Range resume, and no reliable anonymous access to every Instagram story, photo, or profile. Some source formats require login or are no longer public. See [security](docs/SECURITY.md) and [dependency notices](docs/DEPENDENCIES.md).
