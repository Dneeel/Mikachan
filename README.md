# Mikachan

**A private Telegram bot with Downloader, Follow, Community, and Utilities.** One aiogram dispatcher, one SQLite database, one bot identity. Access is denied by default: a registered numeric user ID and an approved group are required for group use. Only Owners can interact in a private bot chat.

The active development generation is **`v2`**. The `v1` branch preserves the earlier downloader generation. This repository does not run a general chatbot or respond to ordinary conversation.

## Follow + social repair (local source patch)

This source package contains the Follow repair described in
[`docs/FOLLOW_DOWNLOADER_REPAIR_FA.md`](docs/FOLLOW_DOWNLOADER_REPAIR_FA.md).
Announcements have two controls: **Get it** (on-demand fetch, subject to
source access) and **View original** (direct platform link). Instagram Reels
are queried separately from Posts; YouTube/TikTok Live checks are experimental.
Some public providers disallow anonymous access; no source can guarantee
all stories, live events, handle changes, or public profile data. A successful
local unit test is not evidence of working live Telegram delivery.

## Install on a server

Use a Debian or Ubuntu server with systemd, `sudo`, internet access, and at least 2 GB free disk. The installer installs host media/OCR tools, retrieves the locked `v2` release, runs offline tests, installs one systemd service, and opens the terminal panel. It **does not start the Telegram bot** until Guided setup is complete.

For a public repository, run this single line in an interactive SSH terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/Dneeel/Mikachan/v2/deploy/bootstrap.sh | sudo bash
```

For a private repository, configure a read-only GitHub deploy key first, then use the private-repository command in the [installation guide](docs/INSTALL_UBUNTU.md). Never place a bot token or private key in the command line or Git remote URL.

When installation finishes, choose **Guided setup**. The panel asks for the BotFather token and your **numeric** Telegram owner ID. Additional user IDs and group IDs are optional; press Enter to skip them. Telegram API ID and API hash are **not required** with the default public Bot API. Setup stores secrets in a root-owned configuration file and starts the bot. Reopening Setup shows a warning and saves a snapshot before applying changes.

Afterward, open the panel at any time with:

```bash
mikachan
```

The panel has ten top-level choices: Overview, Services, Deployment, Configuration, Integrations, Logs, Data & Backups, Appearance, Maintenance, and Exit. It shows the installed branch and exact commit, service state, host CPU, memory and free disk, queue activity, masked settings, and release actions. You can edit the bot token, owner ID, access lists, providers, and other supported settings individually. Same-branch updates work on any `vN` branch; you can switch to another version branch or a reviewed commit, roll back with a database snapshot, back up, restore, inspect logs, or remove the managed installation. The interactive panel opens with a skippable cinematic text intro; appearance preferences and optional custom media/artwork live under `/opt/Mikachan/assets/terminal/`. See the [operations guide](docs/INSTALL_UBUNTU.md) and [asset guide](assets/terminal/README.md).

## What the bot does

| Domain | Implemented interface |
| --- | --- |
| Downloader | Eligible links ask **Yes / No** before creating a durable job. `/search`, `/music`, `/podcast`, `/twitter`, `/instagram`, `/tiktok`, `/youtube`, `/queue`, `/status`, and settings provide discovery and delivery. There is no `/x` command. Instagram media and profile menus require the optional user account session and relay through @instaRoxinoBot. |
| Community | `/fal`, `/poem`, `/quote`, `/dice`; approved groups also have `/rules`, `/setrules`, `/delrules`, `/notes`, `/setnote`, keyword-triggered notes, welcomes, observed message statistics (`/groupstatus`, `آمار`), reply-based `/id` (`آیدی`, `ایدی`), scoped moderation, and nicknames isolated per group. `/groupsettings` is available to an Owner or an explicitly assigned Mikachan Admin; Telegram's native admin badge does not grant bot privileges. `/poem` browses and searches Ganjoor's public catalog; nine Hafez poems from Persian Wikisource are bundled as an offline fallback. Source and rights metadata remain in the content store; the poetry cards omit source footers. Quotes still require reviewed owner imports. `/joke` is disabled. |
| Utilities | `/qr`, `/scan`, `/ocr`, `/weather [CITY]`, `/now [CITY]`, `/nasa`, `/st`, `/xst`, and userbot based `/tr`, `/pron`, `/asr`, `/style`, `/ai`. `/settings` stores a home city and timezone, with coordinates where available, for weather and time defaults. Explicit city arguments affect one request. Persian `هواشناسی [CITY]`, `اکنون [CITY]`, and explicit price requests such as `۱۱۲ دلار` are recognized. Weather includes a modeled US AQI. Iranian Toman quotes cover common currencies, coin, gold, USDT, BTC, ETH and Gram (formerly Toncoin); amounts use validated source quotes. An optional owner-supplied Telegram user session can query CoinPJ for Toman quotes. Global fiat and crypto reference pairs remain separate. `/st` (عکس به استیکر) converts a replied photo or image document, or an image with command caption, to a static WebP sticker. PNG, JPEG, WebP, GIF, BMP, TIFF and other installed Pillow decoders are supported; only the first frame is used for animated images. `/xst` (استیکر به عکس) exports a replied sticker as a transparent PNG document; for animated TGS/video WebM stickers the first frame is extracted (requires the existing animation renderer or ffmpeg). The old `/rate` public command was removed; plain-text currency requests still work through CoinPJ and internal asset lookup. Other legacy image, GIF, sticker-pack, conversion, and `/time` commands remain disabled. |

Commands, eligible links, exact triggers, valid interaction replies, and authorized callbacks are the only ordinary entry points. In groups, a person must be active in the Mikachan registry **and** the group must be approved. Admin controls require an Owner grant for that exact group. Owner `/settings` in private chat includes Access Control, Group Registry, Local Sources and other status pages; personal settings are shared by all roles. Some master-spec sections remain disabled; see the [unified settings ledger](docs/UNIFIED_SETTINGS_STATUS.md). Bot-generated interface text is English; authentic Persian cultural content and exact Persian triggers remain Persian.

`/book TITLE` or `/book` then a title searches active owner-managed Telegram Local Sources for PDF and EPUB files up to 48 MB. `/music` and `/podcast` also search sources marked for those categories and show separate local audio results (MP3, M4A, OGG, FLAC). In private `/settings`, the Owner can add a joined channel/group by negative numeric ID, edit its display name and categories, suspend/resume it, or delete it. The user account must already have joined the source. Search results are bound to the requesting user and expire after 15 minutes; fetched files are validated and deleted from temporary storage after delivery.

## Development and verification

```bash
git clone --branch v2 https://github.com/Dneeel/Mikachan.git
cd Mikachan
uv sync --locked --extra dev
uv run pytest -q
uv run ruff check .
uv run mikachan doctor
```

For a local run, copy `.env.example` to `.env`, set `BOT_TOKEN`, `OWNER_TELEGRAM_ID`, and `ALLOWED_TELEGRAM_IDS` (including the owner), then run `uv run mikachan run`. Set `.env` permissions to `0600`. Do not run a local poller while the systemd service is running for the same bot token.

For the optional Telegram user account features, create a Telegram app API ID and hash for **your user account** and enter both through Configuration in the terminal panel. From the installed root, run `cd /opt/Mikachan/current && sudo ./.venv/bin/mikachan userbot-login`; scan the QR in the terminal to save `USERBOT_SESSION` in the managed configuration. Keep the session string private. The user account serves CoinPJ rate queries, `/proxy` (or `پروکسی`) through @Dnproxbot, and Instagram media/profile requests through @instaRoxinoBot. Instagram links retain Mikachan's Yes/No confirmation. The upstream bot requires at least 29 seconds between queries; Mikachan serializes them and waits 30 seconds. Profile Stories, Highlights, and Posts controls relay only selected actions. If a private `USERBOT_STAGING_CHAT_ID` is configured and both bot identities can use that chat, interactive Instagram media can be copied by Telegram without a server download; staged messages are removed immediately. Otherwise media uses short-lived files under the Mikachan root and is deleted after delivery. A third-party bot receives the submitted Instagram link or username; availability, complete batches, and Telegram live acceptance depend on that provider. The ordinary bot remains the only polling consumer for its bot token. If CoinPJ is unavailable, a clearly labeled public Toman estimate is attempted. Instagram profile posts are forwarded as they arrive from @instaRoxinoBot; the Next 5 posts control remains available after each provider batch.

`/proxy` can show up to six Telegram proxy links as text and generate one QR containing those links in memory. The By country panel includes the upstream Back action. `/link` and a replied `لینک` use `/url`; a replied `Id`, `ID`, or `id` uses the group identity view. `/vpn` opens **MikaSub** and a private **Sub Converter**. MikaSub publishes owner-managed, untested share configs through the existing Cloudflare Worker/KV/D1; there is no proxy ping or connection testing. Owner source controls live in private `/settings` → MikaSub. See the [MikaSub guide](docs/MIKASUB.md) for Cloudflare setup, supported formats, Free-tier budgets, and the intentional YAML-button-to-offline-HTML behavior. OCR and translation require the configured userbot: `/ocr` sends a replied image to @OCRnerdBOT; `/tr LANGUAGE TEXT` or a replied `/tr LANGUAGE` uses @UtilHubBot. `/asr` relays replied audio, while `/pron`, `/style`, and `/ai` also use @UtilHubBot. These third-party bots receive only the requested input.

Current offline checks and known limits are recorded in the [feature ledger](docs/COMMUNITY_UTILITIES_STATUS.md), [system audit](docs/EXISTING_SYSTEM_AUDIT.md), [market research](docs/MARKET_DATA_RESEARCH_2026-10-17.md), [Follow provider investigation](docs/FOLLOW_PROVIDER_RESEARCH_2026-10-20.md), [Pornhub 410 investigation](docs/PORNHUB_410_RESEARCH_2026-10-20.md), and [terminal design review](docs/PANEL_DESIGN_2026-10-18.md). Offline tests and provider probes do not establish Telegram acceptance or unrestricted access from every server. Anonymous social-source monitoring can fail when providers block extractors.

For Follow, the Owner may optionally configure private TikTok or X cookie files using `MIKACHAN_TIKTOK_FOLLOW_COOKIES_FILE` or `MIKACHAN_X_FOLLOW_COOKIES_FILE` in the terminal panel. Files must be owned by the bot service user with no group or public access. These settings affect Follow extraction only; they do not guarantee that stories, live state or timelines will be available. Instagram continues to use its separate optional cookie file for public-source extraction.

## Design

Mikachan keeps downloader, community, and utility handlers inside one Telegram update consumer. SQLite WAL stores authorization, job state, group settings, and reusable Telegram file IDs. Media tools run on demand; utility files stay in memory and downloader artifacts are removed after delivery. Crash leftovers are cleared at the next bot start or through the panel with the service stopped. A managed installation keeps **all Mikachan-owned code, configuration, runtime data, cache, temporary work and local backups under `/opt/Mikachan`**. Only the systemd unit, launcher and OS-managed packages live outside it; the [operations guide](docs/INSTALL_UBUNTU.md) lists the exact paths.

Downloader defaults to three concurrent jobs per user and three globally. A collection occupies one slot so a large playlist leaves room for two separate requests; its panel can stop all remaining queued items. The active item is allowed to finish. YouTube video delivery uses a longer upload timeout and fits oversized source files within the configured Telegram Bot API upload limit where usable quality can be retained. Repeated delivery of the same single item can use Telegram's saved `file_id` without keeping a local video copy. Existing installations with explicit `MAX_ACTIVE_DOWNLOADS` or `MAX_ACTIVE_PER_USER` settings should set both to `3` in Configuration to use the new concurrency default.

[Security](docs/SECURITY.md) · [Dependencies and notices](docs/DEPENDENCIES.md) · [Architecture boundaries](docs/ARCHITECTURE_BOUNDARIES.md)
