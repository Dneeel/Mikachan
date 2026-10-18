# Mikachan architecture and ownership boundaries

This historical handoff describes the pre-integration downloader checkout. For the current integrated `v2` code, see [Existing System Audit](EXISTING_SYSTEM_AUDIT.md) and [Community and Utilities status](COMMUNITY_UTILITIES_STATUS.md). Its downloader ownership notes remain useful, but its implementation and test counts are superseded.

## Current repository map

| Path | Current responsibility | Change owner |
| --- | --- | --- |
| `src/mikachan/__main__.py`, `cli.py` | One `mikachan` CLI; `run` starts the Telegram bot, `doctor` checks local dependencies/configuration. | Integration for future shared bootstrap; downloader for current defects by agreement. |
| `src/mikachan/bot.py` | One aiogram `Dispatcher` and `Router`; command, eligible-link, session-text, and callback handling; starts one polling consumer and the downloader worker. | Shared routing/authorization changes require integration coordination; downloader retains its current handlers until a reviewed contract exists. |
| `src/mikachan/security.py`, `config.py` | Numeric sender allowlist, private-chat policy, URL validation, runtime settings. | Integration for cross-domain auth/config; downloader for provider and URL-specific fixes by agreement. |
| `src/mikachan/store.py` | SQLite WAL access, confirmations, jobs, search sessions, collections, receipts, cache, preferences. | Shared schema/migration decisions require integration coordination; downloader owns existing job behavior. |
| `src/mikachan/search.py`, `catalog.py`, `podcasts.py`, `matching.py`, `music_ui.py` | Federated discovery, catalog identity, podcast resolution, recording matching, lyrics and music UI metadata. | Downloader. |
| `src/mikachan/jobs.py`, `media.py` | Bounded job scheduling, extraction/verification, Telegram media delivery and receipt recovery. | Downloader; future shared job/output contracts require integration review. |
| `tests/` | Existing downloader behavior and adapter regressions. | Downloader for existing tests; future domain owners add their own tests. |
| `pyproject.toml`, `uv.lock`, `.env.example` | Shared dependency and runtime configuration surface. | Serialize edits through integration review. |
| `deploy/`, `docs/INSTALL_UBUNTU.md` | Existing downloader deployment templates and instructions. | Integration/release owner before any integrated deployment. |
| `README.md`, `docs/` | Product overview and current downloader evidence. | Coordinate project-level wording; domain owners maintain their own evidence. |

There is no root `AGENTS.md`, `CONTRIBUTING` file, or tracked GitHub Actions workflow at this handoff. This document does not create empty future modules or a second bot.

## Verified runtime behavior and boundaries

- `App.message` and `App.callback` in `bot.py` check sender authorization before handling updates. `security.allowed` requires a numeric allowlist match and, by default, the sender's private chat. Enabling group context requires a separately reviewed policy; membership in an allowed group alone must never grant access.
- Eligible links create an expiring Yes/No confirmation. `Store.decision` binds the decision to the initiating user, chat, prompt and pending state. No creates no job; the callback deletes the bot's confirmation message when Telegram permits. Other text is ignored unless it is a command or a short-lived search query reply.
- `/twitter` is the supported help command; `/x` is not registered. Search type and source filters are independent. The downloader uses native Telegram messages and callbacks.
- `App.run` calls `Dispatcher.start_polling` and starts one bounded downloader worker. Another agent must not start a second consumer for the same bot identity. No webhook or Discord adapter exists in this repository.
- The CLI reads configuration keys such as `BOT_TOKEN`, `OWNER_TELEGRAM_ID`, `ALLOWED_TELEGRAM_IDS`, `PRIVATE_CHATS_ONLY`, `BOT_API_MODE`, and optional provider credentials. Values belong in private runtime configuration, never source.

## Coordination before adding domains

`v1` is the retired downloader-only baseline; preserve it without further development. `v2` is the **only active development branch** for both Main Codex and Downloader Codex. Commit ordinary fixes, features, and documentation directly to the latest `v2` after checking its current HEAD and reconciling concurrent work. Never create local or remote task, feature, fix, agent, experiment, or PR branches. Version branches are named `v1`, `v2`, `v3`, and so on; only the owner may authorize a new major-generation branch. Do not create `main` or `v3` under the current policy. Before editing the dispatcher, authorization, storage schema, entrypoint, dependencies, environment template, deployment, or project-wide guidance, coordinate with Main Codex as integration owner. If a shared-file conflict cannot be safely reconciled, stop that edit rather than overwrite another agent's work. No production release should run until one reviewed intake path owns each update.

The integration owner should first agree on command/callback ownership, numeric authorization in groups, shared database migrations, a single Telegram intake, and a future output/persona contract. Conceptual seams are an authorization check, one update router/feature registry, event-based wording, a downloader job service, and a Telegram output gateway. These are interface discussions, not implemented classes. Community, Utilities and personality are integrated in `v2`; subsequent feature work follows the same shared routing and authorization boundaries.

## Current identity and release manager

The active package, CLI, SQLite filename and systemd unit use the Mikachan name.
The standalone terminal manager installs one bot release under `/opt/Mikachan`,
with state, configuration, caches, temporary work and backups beneath that same root. It never starts a
second Telegram update consumer. Community and Utilities are integrated in the
same `v2` dispatcher, and downloader internals remain owned by the downloader
maintainer. The retired `v1` branch received only identity cleanup.
