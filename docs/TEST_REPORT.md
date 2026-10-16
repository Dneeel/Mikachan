# Test report — 2026-10-16

Executed on the supplied Freestyle Ubuntu 24.04 VM in `/opt/mika-nexus`:

- `uv sync --extra dev` — installed 42 packages and produced the committed `uv.lock`.
- `.venv/bin/ruff check src tests` — passed.
- `.venv/bin/ruff format --check src tests` — passed.
- `.venv/bin/mypy src` — passed, no issues in 10 source files.
- `.venv/bin/python -m unittest discover -s tests -v` — **15 tests passed**, including the aiogram fake Telegram consent flow.
- `ffmpeg` and `ffprobe` installed from Ubuntu packages; service PATH includes `.venv/bin/yt-dlp`.
- `mika-nexus doctor` with service configuration — valid public Bot API mode, one allowed user.
- Telegram `getMe` — succeeded for `@mikadlbot`.
- Live catalog search for “Daft Punk” with Apple and Deezer — 30 hits across both sources, no failures.
- `systemctl` reported `ActiveState=active`, `SubState=running` before the VM was paused. The VM was then paused to conserve runtime quota.

Still awaiting owner acceptance: a real `/start` conversation, callback approval/rejection, actual media download/delivery, and live checks for all other providers. No local Bot API mode test was possible without Telegram API ID/hash. Deno is not installed.
