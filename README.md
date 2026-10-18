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

The panel has Overview, Settings, Versions, Backups, Service, and System sections. It shows the installed branch and exact commit, service and database health, settings with secrets masked, and available release actions. You can edit the bot token, owner ID, access lists, providers, and other supported settings individually. Same-branch updates work on any `vN` branch; you can also switch to another version branch or a reviewed commit, roll back with a database snapshot, back up, restore, inspect logs, or remove the managed installation. See the [full operations guide](docs/INSTALL_UBUNTU.md).

## What the bot does

| Domain | Implemented interface |
| --- | --- |
| Downloader | Eligible links ask **Yes / No** before creating a durable job. `/search`, `/music`, `/podcast`, `/twitter`, `/instagram`, `/tiktok`, `/youtube`, `/queue`, `/status`, `/cancel`, and settings provide discovery and delivery. There is no `/x` command. |
| Community | `/fal`, `/poem`, `/joke`, `/quote`, `/dice`; approved groups also have rules, notes, custom commands, welcomes, group statistics, moderation, and nicknames isolated per group. Exact Persian triggers such as `فال` and `شعر` are supported. Cultural catalog entries require reviewed, permissioned content; an empty catalog reports unavailable content. |
| Utilities | QR creation/scanning, OCR, sticker and image tools, supported file conversion, weather, dates, APOD, deliberate translation, and market rates. `/rate` and exact triggers including `دلار`, `سکه`, and `ارز` cover Iranian market quotes. Provider failures are reported instead of inventing prices. |

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

Current offline checks and known limits are recorded in the [feature ledger](docs/COMMUNITY_UTILITIES_STATUS.md), [system audit](docs/EXISTING_SYSTEM_AUDIT.md), [market research](docs/MARKET_DATA_RESEARCH_2026-10-17.md), and [Telegram response audit](docs/UX_RESPONSE_AUDIT_2026-10-17.md). Offline tests and a successful service start do not establish that every external provider or Telegram media flow works live. Several providers require credentials or source access, and anonymous source downloads can fail when a provider changes. `mypy src` currently reports pre-existing typing errors in downloader-owned files; release records expose this advisory result.

## Design

Mikachan keeps downloader, community, and utility handlers inside one Telegram update consumer. SQLite WAL stores authorization, job state, group settings, and caches. Media tools run on demand with bounded work and temporary files. Release management uses a separate, stable panel installation so it can inspect or repair a broken bot environment. A managed installation keeps code, state, backups, and secrets in separate directories; the [operations guide](docs/INSTALL_UBUNTU.md) lists them.

[Security](docs/SECURITY.md) · [Dependencies and notices](docs/DEPENDENCIES.md) · [Architecture boundaries](docs/ARCHITECTURE_BOUNDARIES.md)
