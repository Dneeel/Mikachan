# v2.1 temporary branch · integration status

This branch starts at the latest `v2` commit `e7b08a4f70ff` and keeps the
original downloader and Telegram polling service. The owner's uploaded n8n
export was reviewed: Telegram Trigger, Groq AI Agent, Data Table reply memory,
ElevenLabs TTS/STT, and Telegram output nodes. A separate private HTTP
workflow was created in the owner's n8n instance; it has no Telegram Trigger.
The original workflow remains inactive. Live provider results were not
exercised because they can consume account credit.

`/mika` and the exact `میکا` prefix replace `/ai` for the n8n AI path. Numeric
authorization still happens in Mikachan before relay. The workflow chooses
Normal or Mommy tone using the mode selected by the bot from its numeric-ID
configuration. `/tts` and `/stt` use the same private relay when enabled.
The rest of Mikachan stays in its existing runtime. The `v2.1` code is not
installed on the owner's VPS yet, so live Telegram behavior is unverified.

The installer now lists numbered release branches such as `v2.1`; it hides the
legacy `v2-test` branch from ordinary version menus while retaining explicit
access to pinned historical releases. The n8n wizard accepts Enter as the
safe "No, Mikachan Only" choice. The example installation status's
`SETUP REQUIRED` means the bot configuration was not yet entered. CPU load
during n8n installation is a host observation, not proof of a bot fault.
The inherited mypy baseline still has errors in existing modules; no quality
record is rewritten to claim otherwise.

MikaSub's parser accepts up to 10,000 records from a single input document.
The cited VLESS subscription was checked live during development: 5,743
decoded records produced 4,167 unique parsed nodes. This is a read-only
source check, not a test of proxy connectivity. The Cloudflare publication
collection retains its independent 5,000-node limit.

The new Intelligence Desk tracks owner-configured phrases in approved groups,
provides category visibility, word/person counts and TXT/JSON/offline HTML
exports. `/id` now has Summary, Details and owner-bound Manage screens.
Some requested depth remains pending: a live authenticated web viewer, complete
moderation from Manage, and exports larger than the latest 10,000 occurrences.
These screens must not imply older Telegram history was observed. See
`docs/INTELLIGENCE_DESK.md` for the retention and privacy boundary.
