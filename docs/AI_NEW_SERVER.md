# Mikachan AI on a new server

Mikachan remains the only Telegram update consumer. n8n receives authenticated
requests from the bot and never polls Telegram. The installer can install n8n
without any AI credential; the bot's ordinary commands continue working.

## What is automatic

- The independent n8n installer creates its Compose project, encryption key,
  private data directory, access route, HTTPS when a verified domain permits it,
  backups, and management command.
- The versioned Mikachan AI workflow is imported once the n8n owner API key is
  present. If provider credentials are missing, it is imported as an **inactive
  draft**, with foreign credential IDs removed. Attach the credentials in the
  n8n editor and activate it there. When all credentials exist before import,
  the reconciler activates and verifies the graph. Updates back up the previous
  graph and reject unexpected outside edits.
- The reply ledger data table is created when absent and its instance-specific
  ID is mapped into the imported graph. An existing table is checked for the
  required columns before use.
- Future Mikachan and n8n manager updates retry workflow reconciliation when
  the private API key remains available. The key is never kept in Git.

## One-time owner actions

1. Complete Mikachan's Guided setup with the Telegram bot token and numeric
   owner ID. Set up n8n's owner account in the browser at the access URL shown
   by `n8n-manage status`.
2. In n8n, open **Settings → n8n API** and create an API key. Run
   `sudo n8n-manage ai-connect` in the server terminal, then paste the key at
   the hidden prompt. The manager verifies it against the local n8n API and
   writes `/opt/n8n/ai/api-key` with mode `0600`. An invalid key never replaces
   a working one.
3. Add the provider credentials used by the workflow in n8n's credential UI:
   **Groq account**, **OpenRouter account**, **Telegram account**, and the
   credentials required by the owner's existing TTS/STT branches. The workflow
   importer reports `NOT_CONFIGURED` until all referenced credentials exist.
   No API key is placed in the repository or passed on a command line.
4. Check the returned status. `IMPORTED_SETUP_REQUIRED` means the workflow is
   visible in n8n but inactive; its result names the credentials to attach to
   the indicated nodes. Activate it in n8n after completing them. `ACTIVE`
   means the graph imported and activated, not that every external provider
   was live-tested. Test `/mika`, `/tts`, and `/stt` from an authorized ID.

The packaged PostgreSQL schema and style seeds are **preparation only**. The
current n8n service uses SQLite for its own database and the AI agents use
separate Simple Memory nodes. PostgreSQL Memory, the long-term identity store,
and automatic schema seeding still need a managed database, credential wiring,
backup/restore integration, and a migration test. Do not switch the agents to
PostgreSQL Memory by editing the workflow alone.

TikTok `/explore` uses a public third-party feed/search endpoint with no paid
API key. Public discovery can be rate limited or blocked, especially for topic
search. In that case Mikachan reports the source failure; it does not invent a
post or silently change the topic. Telegram receives media directly from the
provider URL, so no explore media file remains on the VPS.
