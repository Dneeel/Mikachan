# Test report — 2026-10-16

Executed on the supplied Freestyle Ubuntu 24.04 VM in `/opt/mika-nexus`:

- `uv sync --extra dev` — resolved 44 packages including `gallery-dl`; `uv.lock` is committed.
- Ruff lint and formatting checks passed; mypy reported no issues in 10 source files.
- `.venv/bin/python -m unittest discover -s tests -v` — **15 tests passed**, including the aiogram fake Telegram consent flow.
- `ffmpeg`, `ffprobe`, `yt-dlp`, and `gallery-dl` are installed in the service environment. `doctor` validated public Bot API mode and the single authorized user.
- Telegram `getMe` succeeded for `@mikadlbot`; `getWebhookInfo` showed no webhook and zero pending updates after polling resumed.
- Live Apple and Deezer search returned 30 results for “Daft Punk” without provider failures.
- Owner-submitted TikTok links produced consent prompts. Two video jobs completed. A TikTok photo post initially failed under yt-dlp; after adding the gallery-dl fallback, the same approved job completed with two Telegram document receipts.
- The service reported `ActiveState=active`, `SubState=running` during the checks. The VM is paused after testing to conserve runtime quota.

Limits: a separate TikTok photo post returned an extractor error indicating that source access or cookies may be required; no owner credentials were requested or used. Deno and local Bot API mode are not configured. Other download providers have not had live acceptance tests.
