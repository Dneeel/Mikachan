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
- If an existing active workflow matches Mikachan's authenticated webhook,
  two-agent, and media contract, the reconciler uses that workflow as its
  source, backs it up, and repairs it under the same workflow ID. Owner voice,
  prompts, notes, valid credentials, Postgres memory and media paths remain.
  A foreign or structurally incompatible workflow follows the separate Setup
  Draft path. The reconciler fills only empty credential slots with exact
  named n8n credentials.
  Once every enabled node has a valid credential and the draft has the approved
  authenticated POST webhook, it backs up both graphs, deactivates the original,
  activates the draft, and verifies the result. A failed activation restores
  the original webhook. Incomplete drafts remain inactive with an exact list of
  missing node credentials; they never take over Telegram delivery.
  A PostgreSQL-backed draft may omit the disconnected Simple Memory nodes.

The independent `n8n-manage` command runs reconciliation as a Python module
from the current Mikachan release, so `deploy.n8n` imports resolve during
both initial import and later repair. `RECOVERABLE` now includes a sanitized
API operation and HTTP status when n8n rejects an import; it never prints an
API key or submitted workflow body.

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
duplicate writes on retry. An isolated live n8n probe verified the Mika and
Serious PostgreSQL chat-memory nodes and an explicit fact write/read on one
instance; the synthetic test fact and identity were removed afterward.
General chat does not automatically create durable facts.

Search requests take one bounded Tavily HTTP request before either Agent. Up to
three titles, URLs and short snippets are passed to a single Groq call; ordinary
chat bypasses Tavily. This avoids the extra Groq calls made by the former MCP
tool path. The Tavily key lives only in n8n's encrypted **Mikachan Tavily Search**
Header Auth credential, restricted to `api.tavily.com`. Connect it once through
**n8n-manage → AI Search · Tavily** or `n8n-manage ai-search-connect` after the
n8n API key is connected. The key is not saved in Mikachan files. Without it,
search prompts receive an explicit unavailable-source context while other AI
routes remain usable. Search snippets are untrusted; the Agent must check dates
and cite returned URLs. Provider quota and Telegram delivery still require live
verification on each installation.

The AI route passes through long-term memory and then the bounded search node.
An earlier import left search nodes present but bypassed them; the reconciler
now reconnects that edge even when the nodes already exist. NASA news queries
prefer the official `nasa.gov` source. General news searches prefer Reuters,
AP, BBC and Al Jazeera, and conversational filler is removed from the query.
Search result relevance and model claims still need review for time-sensitive
subjects.

ElevenLabs TTS uses its v4 REST endpoint through n8n's HTTP Request node with
the existing encrypted ElevenLabs credential and selected voice ID. The
community TTS node returned HTTP 400 for v4 on a live test. A separate Code
node prepares the text and request body; STT remains the ElevenLabs speech
recognition node. The v4 audio, STT transcription and Telegram audio delivery
were verified with short isolated live probes. Longer audio and quota behavior
remain provider-dependent.

Two Cloudflare Workers AI HTTP Request nodes are packaged for Mika and Serious,
using Cloudflare's REST endpoint for `@cf/google/gemma-4-26b-a4b-it`. Groq
remains the default. Under **AI Models → Cloudflare Workers AI**, the manager
can discover the account, verify the token and model listing, and create an
encrypted Header Auth credential in n8n. The token is not stored in Git or
an environment file. The owner may explicitly choose Cloudflare or Groq for
each agent. Cloudflare selection uses a bounded direct HTTP call; it does not
run the LangChain Agent or its Postgres Chat Memory node. The explicit private
fact context still reaches that route. Listing a model does not prove Free
quota or successful inference; test the chosen route before relying on it.

TikTok `/explore` uses a public third-party hashtag lookup followed by that
hashtag's post feed. The one-request-per-second challenge budget is enforced
and returned posts are reused for One More before another fetch. A general
feed is the fallback when a verified hashtag feed is unavailable; fallback
posts must actually mention the requested topic. Public discovery can still be
rate limited or blocked. Mikachan does not invent a post or silently change
the topic. Telegram receives media directly from the provider URL, so no
explore media file remains on the VPS.
