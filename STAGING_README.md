# Mikachan V2 — Isolated Test Branch

**Status: Full combined test source uploaded; installer is version-selectable.**

**2026-10-20 regression recovery:** The reported seven installation-stage test failures
were reproduced and repaired on `v2-test`. The release manager now recognizes its
managed `uv` executable during read-only preflight and refreshes a bootstrap-owned
panel after a verified first install. The incomplete-install path is covered by an
offline resume test. The full suite passes **291 tests**; Ruff, Python compilation
and installer shell syntax pass. Mypy remains advisory with **36 existing errors**
in six source files. No VPS installation or live Telegram delivery was performed
for this repair. See the [safe resume commands](docs/INSTALL_VERSION_SELECTION_FA.md#ادامهٔ-نصب-آزمایشی-پس-از-شکست-آزمونها).

- Production branch: `v2` — unchanged.
- Experimental branch: `v2-test` — includes Follow/Downloader, Owner, CoinPJ, sticker, Notes/Rules, ASCII terminal panel and installer changes.
- The installer supports explicit `--branch` and optional full `--commit`; no version is silently selected.
- The one-command test installer always selects `v2-test`.
- The production branch is NOT automatically merged, switched or deployed.

## Fresh VPS: select a version

```bash
curl -fsSL https://raw.githubusercontent.com/Dneeel/Mikachan/v2-test/deploy/bootstrap.sh | sudo bash
```

## Fresh VPS: install test version directly

```bash
curl -fsSL https://raw.githubusercontent.com/Dneeel/Mikachan/v2-test/deploy/bootstrap-test.sh | sudo bash
```

With no Telegram configuration, the installer stages the code and opens Guided setup. The bot service is started only after successful configuration. A private `0600` environment file may be supplied with `--config` for immediate configured activation.

**Safety:** The bootstrap aborts if a Mikachan installation already exists under `/opt/Mikachan` or its systemd unit is present. It does not support side-by-side installation on a production VPS. Use a separate fresh VPS and a separate Telegram bot token for live testing. Never run two polling instances with the same bot token.

See [docs/INSTALL_VERSION_SELECTION_FA.md](docs/INSTALL_VERSION_SELECTION_FA.md) for exact commands, pinning historical commits, and limitations.

Offline manager/installer tests passed in the build environment. A real VPS/Telegram end-to-end test has **not** been run yet. Do not merge into `v2` without live acceptance.
