# Test report — 2026-10-15

Executed in `/workspace/mika-nexus`:

- `python -m compileall -q src` — passed.
- `PYTHONPATH=src python -m unittest discover -s tests -q` — **14 tests passed, 1 skipped**. Covers URL syntax, mixed DNS rejection, source labeling, private-chat auth, consent race/idempotency/expiry, recovery, search session ownership/generation, filter/source isolation and distinct versions, configuration fail-closed rules, owner-managed allowlist, idempotent collection selection gallery grouping, fair per-user scheduling, and rate limiting.
- `PYTHONPATH=src BOT_TOKEN=123:fake OWNER_TELEGRAM_ID=1 ALLOWED_TELEGRAM_IDS=1 python -m mika_nexus doctor` — passed config validation; found ffmpeg/ffprobe and reported missing yt-dlp/Deno. The token value was a nonfunctional test placeholder.
- `deploy/setup-ubuntu.sh --dry-run` — passed.
- `UV_CACHE_DIR=/tmp/mika-uv-cache uv sync --offline --extra dev` — failed because aiogram is absent from the local cache.
- Network-enabled `uv sync --extra dev` — permission request interrupted twice before package installation; no lockfile or full runtime test was produced.

Not executed: aiogram runtime smoke test, fake Telegram Bot API integration (test present but skipped due to missing aiogram), ruff, mypy, live source probes, dependency vulnerability scan, Ubuntu systemd test, and VPS test. This build has **not** passed the specification's V1 release gate.
