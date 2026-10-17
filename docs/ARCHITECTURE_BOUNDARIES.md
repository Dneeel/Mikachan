# Mikachan architecture and ownership boundaries

This map describes the code that exists today. Mikachan is the parent product; the downloader is the only implemented feature domain. It is not a directory migration plan. The current local downloader checks are 51 passing tests, Ruff and mypy. This revision has not been deployed or accepted against live providers.

## Current repository map

| Path | Current responsibility | Change owner |
| --- | --- | --- |
| `src/mika_nexus/__main__.py`, `cli.py` | One `mika-nexus` CLI; `run` starts the Telegram bot, `doctor` checks local dependencies/configuration. | Integration for future shared bootstrap; downloader for current defects by agreement. |
| `src/mika_nexus/bot.py` | One aiogram `Dispatcher` and `Router`; command, eligible-link, session-text, and callback handling; starts one polling consumer and the downloader worker. | Shared routing/authorization changes require integration coordination; downloader retains its current handlers until a reviewed contract exists. |
| `src/mika_nexus/security.py`, `config.py` | Numeric sender allowlist, private-chat policy, URL validation, runtime settings. | Integration for cross-domain auth/config; downloader for provider and URL-specific fixes by agreement. |
| `src/mika_nexus/store.py` | SQLite WAL access, confirmations, jobs, search sessions, collections, receipts, cache, preferences. | Shared schema/migration decisions require integration coordination; downloader owns existing job behavior. |
| `src/mika_nexus/search.py`, `catalog.py`, `podcasts.py`, `matching.py`, `music_ui.py` | Federated discovery, catalog identity, podcast resolution, recording matching, lyrics and music UI metadata. | Downloader. |
| `src/mika_nexus/jobs.py`, `media.py` | Bounded job scheduling, extraction/verification, Telegram media delivery and receipt recovery. | Downloader; future shared job/output contracts require integration review. |
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

Use separate branches and PRs: `codex/downloader/*`, `codex/community/*`, and `codex/integration/*` are suggested names. Base work on the current `main`; never force-push another agent's branch. Reserve integration review for changes to the dispatcher, authorization, storage schema, entrypoint, dependencies, environment template, deployment, or project-wide agent guidance. No production release should run until one reviewed intake path owns each update.

The integration owner should first agree on command/callback ownership, numeric authorization in groups, shared database migrations, a single Telegram intake, and a future output/persona contract. Conceptual seams are an authorization check, one update router/feature registry, event-based wording, a downloader job service, and a Telegram output gateway. These are interface discussions, not implemented classes. Future community, utility, entertainment and personality work belongs to their designated agents.

## Legacy internal identifiers

`mika_nexus` imports, the `mika-nexus` package/console script, `nexus.sqlite`, and `deploy/mika-nexus.service` remain intentionally intact. Renaming these would affect imports, persistent state and service configuration. The repository and product can be named Mikachan without migrating those runtime identifiers. User-facing bot strings still contain the old label and require a separately reviewed product/UI change; this handoff does not change production bot settings.
