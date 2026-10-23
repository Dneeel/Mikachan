# Mikachan v2 · Content and n8n engineering record

## Existing system audit

The reviewed `v2` baseline was `1d7fb51` (the previous Instagram/userbot utility
change, based on `557f546`). There is one aiogram dispatcher in
`src/mikachan/bot.py`. `App.message` checks the numeric user and approved group
through `Store.can_access`; `App.callback` repeats those checks before dispatch.
Downloader confirmations, queues, delivery and file cache live in
`src/mikachan/store.py` and `jobs.py`. The Userbot relay is separate in
`userbot.py`. Follow, MikaSub, Community and Utilities have their own modules.
`SettingsUI` already binds owner menus to user, chat, message and expiry.

The original `/quote` route in `community.py` used the local reviewed-content
table. No `/pishi`, `/earth`, `/art`, `/xotd`, `/wiki`, `/tts` or `/stt` routes
or n8n client existed. SQLite is the only application database, with WAL and
additive `CREATE TABLE IF NOT EXISTS` initialization. The terminal manager
owns the installation tree and validates settings against the release's code.
No n8n workflow, webhook, provider entitlement, ElevenLabs credential or live
VPS access was supplied for this phase.

The inherited static check already reported 36 mypy errors in six existing
files. It is not a clean baseline. The pre-phase full regression suite passed
385 tests and Ruff passed. No live bot or workflow was contacted.

## Integration map

| Interface | New responsibility | Existing boundary |
|---|---|---|
| `bot.py` | Route six content commands and `/tts`, `/stt` through the existing dispatcher | Numeric allowlist, group access, Downloader and Userbot routes retained |
| `content_ui.py` | Actor/chat/message-bound content navigation and cat review pool | Same SQLite connection; no second polling process |
| `content_providers.py` | Source-verified media, article, quote and date lookups | HTTP host allowlist, bounded responses; no AI fabrication |
| `n8n_gateway.py` | Single versioned authenticated workflow contract | n8n disabled by default; provider keys stay with owner workflow |
| `audio_ui.py` | Explicit model-aware settings and request gating | No generation until verified catalog, reported free quota and Generate tap |
| `settings_ui.py` | Owner-only cat review and safe n8n test | Existing Access/Group/Local Source controls retained |
| `deploy/manager.py` | Expose new non-secret configuration references | No live deployment or service mutation |

## Source checks on 2026-10-23

Read-only requests from the development environment returned usable JSON/HTML
from TheCatAPI, CATAAS, Cleveland Museum of Art, iNaturalist, Wikipedia,
Wikiquote, Wikimedia Commons and Wiktionary. A historic daily record dated
2026-10-20 was used to check the parsers. The former Met collection search
endpoint returned HTTP 410 with a retirement notice, so it is not used.
Art Institute of Chicago's search endpoint returned an HTTP 403 challenge
from this environment; Cleveland is the enabled art source. These checks do
not establish live VPS reachability, source stability or account entitlement.

## Owner's n8n workflow handoff

Configure `MIKA_N8N_ENABLED=true`, an HTTPS origin (or private loopback HTTP)
in `MIKA_N8N_BASE_URL`, a relative production path in
`MIKA_N8N_WEBHOOK_PATH`, and `MIKA_N8N_AUTH_REF` pointing to a bot-user-owned
mode `0600` secret file inside the Mikachan root. Use a different secret from
ElevenLabs and do not place it in Git, Telegram or callback data. The workflow
must enforce the same shared secret, request ID idempotency, free-budget guard,
TTL file references, result MIME limits and no paid fallback. Do not add a
Telegram Trigger with Mikachan's token.

The JSON request schema is `mika.n8n.v1`: `schema_version`, `request_id`,
`task`, `context`, `input`, `options`. The `X-Mikachan-Auth` header carries the
secret. Catalog tasks are `catalog.capabilities`, `catalog.models` and
`catalog.voices`. Catalog responses use `status=succeeded` and
`result={"kind":"catalog","items":[...]}`. Eligible models and voices
must have real `id`, `name`, and `free_entitled=true`; models describe their
actual `controls`, `languages`, `supports_language_code`, `timestamps`,
`supports_audio_events`, and `output_formats`. The capabilities result must
also include `free_quota_source="provider_reported"` and a measured positive
`free_remaining` before generation can begin. A marketing plan estimate does
not qualify.

`tts` and `stt` submit through the same webhook. Synchronous responses are
`succeeded` with a typed result. Longer jobs return `accepted` and an opaque
`job_id`; `job.status` and `job.cancel` handle bounded polling. For STT, a
multipart `file.upload` request includes `manifest` JSON and binary `media`;
it returns `result.kind=upload` with an opaque `file_ref`. For TTS output,
`file.fetch` on the same authenticated endpoint returns raw MP3/OGG bytes,
not JSON or a permanent public URL. Both endpoints cap the file at 20 MB.
Every JSON response echoes the request ID and schema version. The owner may
map an existing workflow's nodes to this contract; do not overwrite the
existing workflow without the owner's handoff and authorization.

`MIKA_TTS_ENABLED` and `MIKA_STT_ENABLED` remain false until the real workflow,
catalogs, media transfer and free entitlement pass live tests. The cat pool is
empty on first install; the owner can review and approve candidates under
`/settings` → Cat Approval Pool. Unreviewed cat images are never shown.

## Open acceptance gaps

The content and audio screens provide real source-backed or deliberately
gated paths, but the full 925-line specification has additional depth to
finish: source metadata and license display for every media subtype, broader
art and scientific nature catalogs, richer historical date controls, stronger
quote theme search, and model-specific advanced audio controls. Accepted n8n
jobs have restart recovery, but interrupted synchronous work without a job ID
cannot be resumed. Preset, subtitle and recovery paths are built but not live-tested
against the owner's provider. No owner workflow or account evidence has been
provided to validate n8n, ElevenLabs, free quota, audio upload or result
delivery. Existing `/ai` remains the pre-existing Userbot relay; no new public
AI command or image generation interface was chosen here.

Production rollout, service restart, n8n import and any measured provider
generation remain pending explicit owner authorization and account handoff.
