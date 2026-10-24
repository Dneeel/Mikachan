# n8n live workflow audit · 2026-10-24

Read-only source: live workflow `PJx1736XYmyeof7h` (44 nodes, active), backed
up before edits outside the repository. The original workflow was owner-edited;
its prompts, media paths, tool nodes and credentials were retained. One Chat
result normalizer was added; the revised graph has 45 nodes and was activated.

## Verified graph facts

- One authenticated Private Webhook receives the bot relay. There is no n8n
  Telegram Trigger polling for updates.
- Mika and Serious have separate Groq model nodes and separate Simple Memory
  nodes. The Postgres Chat Memory node is disabled and disconnected.
- Tavily MCP and Tavily MCP1 connect by `ai_tool` to Mika and Serious. Tavily
  MCP2 has no active agent connection.
- The Serious Agent already had `continueOnFail` and a result normalizer. The
  Mika Agent lacked both. They were added so an Agent failure has a visible
  fallback route to Telegram.

## Execution evidence

Recent execution records included Groq rate limits, an invalid Agent request,
one missing prompt, and a Simple Memory sub-node failure. These are observed
causes of missing Chat responses. The records do not establish that Tavily was
the sole cause. A synthetic Serious execution reached Telegram delivery but
failed because its test chat was invalid. After the live edit, the available
n8n API keys returned HTTP 401, preventing further live search tests.

## Every Sticky Note

| Note | Owner request | Current state |
| --- | --- | --- |
| Sticky Note | English setup guide with API links and reasons | Updated with actual minimum credentials and links; complete automatic setup pending. |
| Sticky Note7 | ElevenLabs setup, voice/model limits, optional multiple keys | Updated with key link and plan warning; automatic key rotation pending. |
| Sticky Note8 | Better memory on the server | Updated truthfully; managed PostgreSQL and durable memory pending. |
| Sticky Note9 | NVIDIA API and verification | Updated; optional NVIDIA node disabled. |
| Sticky Note10 | OpenAI API link | Updated; optional OpenAI node disabled. |
| Sticky Note11 | OpenRouter API link | Updated; optional OpenRouter node disabled. |
| Sticky Note12 | Better server search | Updated with actual Tavily wiring and observed errors; fallback provider pending. |
| Sticky Note13 | Groq API link | Updated; both active Agents use Groq. |

## Pending acceptance work

Managed PostgreSQL, writer/retriever, selected memory backend, automatic
webhook secret provisioning, independent provider selection, Cloudflare Workers
AI free-tier verification, and post-edit live Telegram tests are incomplete.
Do not label the workflow `AI_READY` solely because its webhook is active.
