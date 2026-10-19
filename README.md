# Mikachan

**A private Telegram bot with three integrated domains:** Downloader, Community, and Utilities. One aiogram dispatcher, one SQLite database, one bot identity. Access is denied by default: the owner explicitly allows numeric user IDs, and groups also need their own authorization.

The active development generation is **`v2`**. The `v1` branch preserves the earlier downloader generation. This repository does not run a general chatbot or respond to ordinary conversation.

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

The panel has six sections: Overview, Deployment, Service, Configuration, Data & Storage, and Maintenance. It shows the installed branch and exact commit, service state, host load, memory and free disk, queue activity, masked settings, and release actions. You can edit the bot token, owner ID, access lists, providers, and other supported settings individually. Same-branch updates work on any `vN` branch; you can switch to another version branch or a reviewed commit, roll back with a database snapshot, back up, restore, inspect logs, or remove the managed installation. See the [full operations guide](docs/INSTALL_UBUNTU.md).

## What the bot does

| Domain | Implemented interface |
| --- | --- |
| Downloader | Eligible links ask **Yes / No** before creating a durable job. `/search`, `/music`, `/podcast`, `/twitter`, `/instagram`, `/tiktok`, `/youtube`, `/queue`, `/status`, `/cancel`, and settings provide discovery and delivery. There is no `/x` command. |
| Community | `/fal`, `/poem`, `/quote`, `/dice`; approved groups also have rules, notes, custom commands, welcomes, group statistics, moderation, and nicknames isolated per group. Exact Persian triggers `فال` and `شعر` are supported. `/poem` browses and searches Ganjoor's attributed public catalog; nine attributed Hafez poems from Persian Wikisource are bundled as an offline fallback. Quotes still require reviewed owner imports. `/joke` is disabled. |
| Utilities | `/qr`, `/scan`, `/ocr [fa\|en]`, `/weather CITY`, `/rate ASSET IRT`, `/now [CITY]`, `/apod`, and deliberate `/translate`. Persian `هواشناسی CITY`, `اکنون [CITY]`, and explicit price requests such as `۱۱۲ دلار` are recognized. Weather includes a modeled US AQI; `/now` stores each user's city and calendar choices. Iranian Toman quotes cover common currencies, coin, gold, USDT, BTC, ETH and Gram (formerly Toncoin); amounts use validated source quotes. Global fiat and crypto reference pairs remain separate. Image, sticker, GIF, conversion, and `/time` commands are disabled for now. |

Commands, eligible links, exact triggers, valid interaction replies, and authorized callbacks are the only ordinary entry points. In groups, a user must be personally allowed **and** the group must be allowed. Bot-generated interface text is English; authentic Persian cultural content and exact Persian triggers remain Persian.

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

Current offline checks and known limits are recorded in the [feature ledger](docs/COMMUNITY_UTILITIES_STATUS.md), [system audit](docs/EXISTING_SYSTEM_AUDIT.md), [market research](docs/MARKET_DATA_RESEARCH_2026-10-17.md), and [terminal design review](docs/PANEL_DESIGN_2026-10-18.md). Offline tests and provider probes do not establish Telegram acceptance or unrestricted access from every server. Anonymous social-source monitoring can fail when providers block extractors.

## Design

Mikachan keeps downloader, community, and utility handlers inside one Telegram update consumer. SQLite WAL stores authorization, job state, group settings, and reusable Telegram file IDs. Media tools run on demand; utility files stay in memory and downloader artifacts are removed after delivery. Crash leftovers are cleared at the next bot start or through the panel with the service stopped. A managed installation keeps **all Mikachan-owned code, configuration, runtime data, cache, temporary work and local backups under `/opt/Mikachan`**. Only the systemd unit, launcher and OS-managed packages live outside it; the [operations guide](docs/INSTALL_UBUNTU.md) lists the exact paths.

Downloader defaults to three concurrent jobs per user and three globally. A collection occupies one slot so a large playlist leaves room for two separate requests; its panel can stop all remaining queued items. The active item is allowed to finish. YouTube video delivery uses a longer upload timeout and fits oversized source files within the configured Telegram Bot API upload limit where usable quality can be retained. Repeated delivery of the same single item can use Telegram's saved `file_id` without keeping a local video copy. Existing installations with explicit `MAX_ACTIVE_DOWNLOADS` or `MAX_ACTIVE_PER_USER` settings should set both to `3` in Configuration to use the new concurrency default.

[Security](docs/SECURITY.md) · [Dependencies and notices](docs/DEPENDENCIES.md) · [Architecture boundaries](docs/ARCHITECTURE_BOUNDARIES.md)
