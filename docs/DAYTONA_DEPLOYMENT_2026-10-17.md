# Daytona deployment check — 2026-10-17

The public GitHub `v2` branch was cloned into `/home/daytona/Mikachan` on a
Daytona Debian 13 sandbox. Python 3.12, the locked `uv` environment, FFmpeg,
Deno, yt-dlp, gallery-dl, Tesseract with English and Persian data, and CairoSVG
were installed. The private `.env` contains the bot token and only the numeric
owner allowlist; it is mode `0600` and is not tracked by Git. Groups remain
disabled until specific group IDs are approved.

On the sandbox, `pytest -q` passed 63 tests, `ruff check .` passed, and
`mypy src` passed. `mika-nexus doctor` found all required media and OCR tools.
Telegram `getMe` matched the intended bot, `getMyCommands` returned the 25
registered commands, `getWebhookInfo` showed no competing webhook, and an
outbound message to the owner succeeded. A single polling process established
HTTPS connections to Telegram. A genuine owner-originated command response,
Telegram media upload, group moderation, and downloader delivery have not yet
been accepted end to end.

The Daytona account's tier restricts network egress. Open-Meteo, Frankfurter,
CoinGecko, NASA, and YouTube requests from the sandbox failed with TCP resets.
Daytona rejected a network-settings update with: `Network access is restricted
and cannot be overridden at the sandbox level`. Weather, rates, APOD, and
remote downloader sources therefore cannot be declared operational on this
server, despite successful offline checks and prior read-only probes elsewhere.
Translation still needs an owner-chosen HTTPS provider; cultural features still
need a reviewed, licensed content import. No unsupported success response is
shown for these features.

This sandbox has no systemd. Run the single polling consumer in the foreground
with `deploy/daytona-run.sh` from the project directory. For a detached run,
use `nohup ./deploy/daytona-run.sh > data/bot.log 2>&1 < /dev/null &`.
The script uses `flock` to prevent a second poller. Stop the process before
stopping the sandbox. The sandbox was stopped after verification to conserve
the owner's limited runtime; the bot will be offline until it is restarted.
