# Mikachan v2 · start to finish

Use an interactive Ubuntu/Debian SSH terminal. The installer keeps Mikachan
under `/opt/Mikachan` and optional n8n independently under `/opt/n8n`.
It does not require an AI key before the bot can run.

## New server

```bash
curl -fsSL https://raw.githubusercontent.com/Dneeel/Mikachan/v2/deploy/bootstrap.sh | sudo bash
```

Choose **v2**, optionally install n8n, and select a verified domain for n8n
if the bot will use its public HTTPS webhook. The domain's DNS A record must
already point to this server. Finish **Guided setup** in the Mikachan panel:
enter the BotFather bot token and numeric Owner ID; add groups and the
Telegram user session only if those features are wanted.

Open the n8n URL shown by `sudo n8n-manage access`, finish its browser
onboarding, and create an owner API key under **Settings → n8n API**. Then:

```bash
sudo n8n-manage ai-connect
sudo n8n-manage ai-auto-setup
```

The first command asks for the API key without showing it and installs the
versioned workflow and managed PostgreSQL memory where resources allow.
The second prepares the private webhook credential and connects the bot only
after the workflow is active and webhook authentication verifies. Add any
provider credentials named in the returned `IMPORTED_SETUP_REQUIRED` result
inside n8n, then run `sudo n8n-manage ai-auto-setup` again. For search, run
`sudo n8n-manage ai-search-connect` and enter a Tavily key at its hidden prompt.
No secret belongs in a command line or Git.

Check:

```bash
sudo mikachan status
sudo n8n-manage status
sudo n8n-manage ai-auto-setup
```

`WEBHOOK_READY` proves local webhook authentication and bot configuration;
it does not prove Groq, ElevenLabs, Tavily, Cloudflare, or Telegram delivery.
Send `/help`, `/mika hello`, `/tts hello`, and a replied `/stt` from an
authorized Telegram ID to verify those separate paths.

## Existing installation on v2

```bash
sudo mikachan update
sudo n8n-manage ai-auto-setup
```

If the VPS still reports branch `v2.1`, run `sudo mikachan switch v2` first.
The temporary branch was removed after its code was merged into `v2`;
`sudo mikachan update` on the deleted branch cannot fetch a new release.

The Mikachan update refreshes the owned n8n manager without restarting n8n.
The reconciler backs up a recognized owner workflow and repairs its AI route
on the same workflow ID; voice, prompts, valid credentials, notes and media
paths are retained. If the n8n API key is absent, run `sudo n8n-manage
ai-connect` first. If the status says `RECOVERABLE`, use `sudo n8n-manage
diagnostics` and read the returned sanitized HTTP operation/status; do not
delete either workflow or its credentials.

If you created a newer n8n API key, run `sudo n8n-manage ai-connect` and paste
that key at the hidden prompt before `sudo n8n-manage ai-auto-setup`. An active
workflow with no executions after `/mika` means the bot relay is still not
connected. Use the auto-setup result to repair that link; do not import a
second copy of the workflow.
