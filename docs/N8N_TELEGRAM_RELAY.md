# Private n8n relay for the owner's existing AI workflow

The importable workflow is generated from the owner's 2026-10-24 export with
`python deploy/build_n8n_relay.py SOURCE.json OUTPUT.json`. The generated
workflow has a **Private Webhook** instead of a Telegram Trigger. Mikachan is
the only consumer of bot updates. The existing AI, TTS and STT provider nodes
still use n8n's Telegram credential to **send** completed results; sending
messages does not take over update delivery.

## Rollout

1. Import the generated JSON into the same n8n instance as the original
   workflow. Select the existing Telegram, ElevenLabs, Groq and Data Table
   resources if n8n does not bind them automatically. A separate workflow
   already exists on the owner's instance as ID `9iWJpA2GTezJfSla`; do not
   import another copy there.
2. On **Private Webhook**, create a Header Auth credential with name
   `X-Mikachan-Auth` and a new randomly generated secret of at least 32
   characters. The secret must match the contents of a bot-user-owned `0600`
   file within `/opt/Mikachan`, such as `/opt/Mikachan/config/n8n-relay.secret`.
   Never paste it into a Telegram message or commit it to Git. The owner's
   n8n instance already has this credential. Its corresponding secret is in
   `/workspace/Mikachan-n8n-relay.secret` in the development workspace and
   must be transferred privately to the VPS through the hosting console.
3. Turn off the old Telegram Trigger workflow before activating the new one.
   The old workflow was inactive when checked; the new one was activated and
   its Header Auth endpoint rejected an unauthenticated request with HTTP 403.
   Verify that the production path is
   `/webhook/mikachan-relay` (not `/webhook-test/...`).
4. In the Mikachan terminal manager, set `MIKA_N8N_ENABLED=true`,
   `MIKA_N8N_TELEGRAM_RELAY=true`, `MIKA_N8N_BASE_URL` to the n8n HTTPS origin,
   `MIKA_N8N_WEBHOOK_PATH=/webhook/mikachan-relay`, and `MIKA_N8N_AUTH_REF` to
   the secret file. Leave `MIKA_TTS_ENABLED` and `MIKA_STT_ENABLED` off in this
   legacy relay mode; those flags belong to the separate `mika.n8n.v1` catalog
   interface. Restart Mikachan. When relay mode is enabled, startup removes a
   stale Telegram webhook **without dropping pending updates** before polling.
5. Verify `/help`, `/tts TEXT`, `/tts` in reply to text, `/stt` in reply to
   audio, `/mika QUESTION`, and the explicit `میکا` AI trigger. Check that
   unauthorized numeric IDs and unapproved groups receive no response.

The attached owner workflow performs the provider calls and sends the final
Telegram output itself. The relay accepts the webhook's immediate HTTP
acknowledgement, not a completed audio result. n8n executions should be
inspected for provider errors. Live ElevenLabs calls may consume account
credit; inspect provider entitlement before activating those paths.

There is no SSH access to the current VPS from the development environment,
so this change has not been deployed or verified against the live bot. The
hosting console or another authorized deployment channel is needed to place
the `0600` secret file and update the service configuration. Merely importing
the workflow does not complete the rollout.
