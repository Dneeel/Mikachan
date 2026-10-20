# Mikachan V2 — Isolated Test Branch

**Status: Branch created; revised source archive NOT yet committed.**

- Production branch: `v2` — leave unchanged.
- Test branch: `v2-test`.
- Initial base commit: `06eed200f41e9d01214c099ff029638ab8bda3a3`.
- Intended source artifact: `Mikachan_V2_Notes_Rules_Final.zip` from the ChatGPT conversation.
- Artifact SHA-256: `5c6e54b28381388b9a8bd04834565f36cc11ae8ee7c4793c5951f5e8bd5fba54`.

## Safe publishing procedure

1. Download the above ZIP from the conversation and extract it locally or into an isolated Codex workspace.
2. Check out `v2-test`, **never `v2`**.
3. Copy the extracted project files to the checkout root (do not nest the extracted top-level folder).
4. Review `git status` and `git diff`; ensure no `.env`, userbot sessions, secret keys, databases, runtime media or tokens are staged.
5. Commit and push to `v2-test`.
6. Run tests and deploy on a separate VPS/install path and separate SQLite database, using distinct Telegram bot credentials if you intend to run alongside production.
7. Only after acceptance, decide whether to merge the tested changes into `v2`; do not force push.

**Do not deploy this branch yet:** this branch currently contains the production source plus this staging guide, *not* the repaired source files.
