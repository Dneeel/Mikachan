# Architecture

`bot.py` handles authorized Telegram updates and callback ownership. `security.py` validates URL syntax before consent and DNS at fetch time. `store.py` holds all transactional SQLite state. `search.py` federates catalog requests with bounded result count. `jobs.py` schedules two jobs at a time, runs extractors on demand, streams direct files, and records delivery receipts.

Consent is inserted before prompt send, then the prompt ID is bound. A callback must match user, chat, message, pending state and expiry. The acceptance update and job creation share a transaction. Crash recovery moves interrupted downloads to `RETRYABLE` and interrupted uploads to `DELIVERY_UNCERTAIN` so uploads are not automatically duplicated.

The app currently retains only each job's immediate workspace while running. Successful Telegram file IDs are cached per user and exact URL/document variant. Search result callbacks reference stored generation and position. A collection's selected item inherits the original accepted collection event and creates an individual job.

Admin allowlist changes use an owner-only, expiring confirmation. Search filter preferences persist per user. Multi-asset uploads use valid 2–10 document groups with receipts per delivered message.
