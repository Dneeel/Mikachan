# Mikachan v2.1 system audit — 2026-10-25

This is a source and offline test audit of `v2.1` at `881c2f7` plus the fixes in
this review. It does not claim a live Telegram, n8n, Cloudflare, or VPS acceptance
test. The repository contains about 26,000 lines of first-party Python across
`src/mikachan` and `deploy`; the review combined module inspection, static
analysis, syntax checks, workflow graph validation, and regression tests.

## Runtime map

- `src/mikachan/cli.py` loads `Settings` and starts `App`.
- `src/mikachan/bot.py` owns the sole aiogram dispatcher. `message` and
  `callback` enforce numeric user and approved chat access before routing
  commands, confirmations, sessions, and callbacks. Group observation is a
  separate intentional path for statistics and owner-defined keyword records.
- `src/mikachan/store.py` stores durable confirmations, jobs, search sessions,
  file IDs, and access state in SQLite WAL. `AccessRegistry` in `access.py`
  enforces active user and group status. `Worker` in `jobs.py` runs up to the
  configured bounded number of jobs and removes job artifacts in `finally`.
- `src/mikachan/instagram_relay.py`, `userbot.py`, `follow.py`, and `explore.py`
  depend on external Telegram accounts or public providers; upstream access
  and throughput are not guaranteed by offline tests.
- `deploy/manager.py`, `manager_ui.py`, `n8n_manager.py`, and `deploy/n8n/*`
  manage separate Mikachan and n8n lifecycles. n8n requires its own credentials
  and an active workflow. `cloudflare/mikasub/src/index.mjs` is the MikaSub
  Worker with KV/D1 state.

## Corrections in this review

- Guarded optional Telegram sender, photo, and message fields in group notes
  and the user account file-link path. These previously had unchecked dereferences.
- Centralized extraction of Telegram media file IDs during delivery and cache
  recording, with explicit checks for absent media fields.
- Rejected HTML disguised as a PDF and incomplete ZIP archives before document
  delivery; genuine ZIP structure is checked without extracting its contents.
- Prevented an empty X text post from dereferencing a missing delivery receipt.
- Removed a Transcript button that only led to a “coming soon” response. Old
  buttons now explain the supported `/asr` reply workflow.
- Replaced the Owner Settings identity placeholder with an accurate identity
  and personality page. Removed a retired metadata filename reference.
- Resolved all 39 baseline mypy diagnostics, including variable type collisions
  in search, settings, and lyrics branches.

## Verification

| Check | Result |
| --- | --- |
| Python regression suite | 515 passed; one Pillow decompression warning from an oversized-image rejection test |
| Ruff | Passed |
| mypy, including `--check-untyped-defs` | Passed on 60 source files |
| Python compile and shell syntax | Passed |
| MikaSub Worker Node tests | 3 passed |
| Bundled n8n workflow graph | 51 unique nodes, no dangling edges, one private Webhook trigger |

## Remaining risks and evidence limits

- HTTP download and avatar paths validate DNS before connecting, but some use a
  second resolver inside the HTTP client. A hostile domain that changes DNS
  answers between those steps could bypass the preflight. The subscription
  fetcher uses a pinned public resolver; other paths need an equivalent
  transport-level guard before arbitrary direct URLs can be considered fully
  protected against DNS rebinding.
- Public social monitoring depends on services that sometimes reject anonymous
  access. Offline Follow tests do not establish reliable Instagram/X story,
  timeline, or live monitoring. Instagram relay behavior depends on
  `@instaRoxinoBot` and its rate limits.
- YouTube, Pornhub, XNXX, large Telegram uploads, n8n credentials, PostgreSQL
  container health, and Cloudflare Worker deployment were not exercised against
  the owner's live environment in this review. Per-provider speed and media
  integrity need separate measurements from the actual VPS.
- The owner-defined keyword ledger stores matching message excerpts in SQLite
  without an automatic retention limit. Disk use and privacy policy should be
  reviewed before enabling large term lists in busy groups.
- The owner has shared live secrets in prior messages. This audit did not copy
  them into source or logs. Their continued use is an operational decision; a
  code test cannot establish whether those secrets remain private.

The audit makes no production-readiness claim for unavailable external systems.
