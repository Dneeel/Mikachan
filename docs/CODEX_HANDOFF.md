# Handoff

Repository path: `/workspace/mikachan`. No GitHub remote or VPS operation was performed in this phase.

Before staging: obtain permission for package network access, run `uv sync --extra dev`, commit the generated `uv.lock`, run the full test/ruff/mypy/audit suite, and resolve any compatibility issues. Configure a staging bot token and owner numeric ID in a 0600 `.env`; keep public Bot API mode for the first smoke test. Execute the live acceptance matrix and record actual provider behavior. The current code is a development build; read `SECURITY.md` before putting it on a shared host.
