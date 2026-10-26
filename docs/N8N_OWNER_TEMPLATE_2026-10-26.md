# Owner n8n template review · 2026-10-26

The owner supplied a 50-node `N8N Mikachan.json` export for offline review.
It is a reference for the current n8n instance, **not** a deployable artifact:
its credential IDs, data-table ID, workflow ID, and voice ID belong to that
instance. No live n8n endpoint was contacted for this review.

The export contains the authenticated `POST /webhook/mikachan-relay` entrance,
one Telegram delivery path, Mika and Serious agents, Groq models, two active
Postgres Chat Memory nodes, the scoped explicit-fact chain, image and
ElevenLabs TTS/STT routes, three Tavily MCP nodes, and owner-authored notes.
Its Memory note still describes Simple Memory and says PostgreSQL is not
installed, while the exported graph has active PostgreSQL nodes. This is a
documentation conflict in the template; the live database was not inspected.
The portable package contains a bounded Tavily HTTP search path and disabled
Cloudflare HTTP Request alternatives. Reconciliation maps instance credentials
and tables through the n8n API. An edited live graph must be backed up before
reconciliation; the package must not silently replace owner voice, prompts,
credentials, notes, or media edges.

The offline `build_ai_workflow.build()` check accepted all 50 owner nodes and
preserved the TTS voice parameters and image/STT edges. Applying the bounded
search transform removed the three MCP nodes and produced 51 nodes while
retaining both Postgres memory nodes and the explicit-fact chain. This checks
graph structure only; it does not prove credentials, model quota, executions,
search accuracy, or Telegram delivery on the live n8n server.

The Cloudflare HTTP route is optional and currently bypasses the LangChain
Postgres Chat Memory node. The explicit private fact context still flows to
that route. Select Groq to keep Postgres Chat Memory until a compatible
Cloudflare conversation-history route is implemented and tested.
