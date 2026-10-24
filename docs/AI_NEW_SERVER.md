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

For a fresh installation, **AI Integration → Auto Setup / Repair** in
`n8n-manage` creates a missing private Header Auth credential and a matching
bot-owned `0600` relay secret. It preserves any existing credential or secret.
Once all provider credentials are present and the workflow activates on a
verified HTTPS domain, it connects the bot through the existing Mikachan
manager. The manager sends an invalid, harmless webhook payload twice to
verify that a wrong header is rejected and the stored secret is accepted
before changing the bot configuration. `WEBHOOK_READY` confirms local header
authentication and bot configuration; provider responses still need testing.

The independent n8n manager now has **AI Memory · PostgreSQL** with Quick
Install/Repair, Status, Backup, Restore and a Simple/PostgreSQL backend choice.
Quick Install creates a private database container on n8n's internal Docker
network and applies the packaged schema and style seeds. The next workflow
reconciliation creates a dedicated n8n PostgreSQL credential and wires separate
chat-history tables to Mika and Serious. The existing Simple Memory path remains
selectable. The PostgreSQL path also installs a scoped writer and retriever:
in a **private chat**, an explicit `remember: ...` or `یادت باشه ...` addressed
to Mika stores a bounded user assertion. Only that numeric sender's private
facts are retrieved for that sender in a private chat. Group prompts never
receive private facts. These assertions are labelled unverified data, not
instructions or independently established truth. Database uniqueness prevents
duplicate writes on retry. The workflow and container are offline tested;
neither has been exercised on the owner's VPS, so live memory behavior remains
unverified. General chat does not automatically create durable facts.

Tavily remains the primary search tool. Each agent also receives a bounded
Wikipedia search for background and Google News RSS for headline discovery,
both without credentials. These are narrower than a general live web search.
The prompt requires source and date checks; a failed tool should still yield a
visible limited response through the existing result normalizer. Live Agent
tool-calling behavior remains provider dependent.

Two Cloudflare Workers AI model nodes are packaged for Mika and Serious,
using `@cf/google/gemma-4-26b-a4b-it`. Groq remains the default. Under
**AI Models → Cloudflare Workers AI**, the manager can discover a single
Cloudflare account, verify the token and model listing, and create an n8n
credential with the account's OpenAI-compatible endpoint. The token stays in
n8n's encrypted credential store, not Git or an environment file. The owner
may then explicitly choose Cloudflare or Groq for each agent independently.
The model does **not** switch on credential creation. Listing a model does not
prove that inference fits the account's Free allowance or that this model can
call the current tools; those need a controlled live check before selection.

TikTok `/explore` uses a public third-party feed/search endpoint with no paid
API key. Public discovery can be rate limited or blocked, especially for topic
search. In that case Mikachan reports the source failure; it does not invent a
post or silently change the topic. Telegram receives media directly from the
provider URL, so no explore media file remains on the VPS.
