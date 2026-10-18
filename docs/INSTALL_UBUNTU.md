# Mikachan v2 on a Debian/Ubuntu systemd server

The terminal manager is the supported path for **new panel-managed installations**.
It uses one systemd service and one Telegram polling consumer. The bot remains a
single process; the panel runs only when an operator opens it.

## Start the panel

```bash
git clone --branch v2 --single-branch https://github.com/Dneeel/Mikachan.git Mikachan
cd Mikachan
sudo bash deploy/manage.sh
```

The launcher needs `python3` and `python3-venv` and creates a private manager
environment at `/var/cache/mikachan-panel/venv`. It installs pinned `textual` and
`uv` there. `sudo bash deploy/manage.sh preflight` is dependency-free and works
before that bootstrap. The bot's Python environment is separate from the panel.

Choose **Preflight** and, on Debian/Ubuntu, **Install OS tools** if media/OCR
dependencies are missing. The panel checks for systemd, Git, uv, flock, ffmpeg,
ffprobe, Tesseract with Persian data, Cairo, and at least 2 GB free disk. It
cannot install a systemd service in a container or sandbox without systemd as
PID 1. The host must also reach Telegram and approved providers; the panel
cannot change a cloud account's network policy.

Choose **Install v2** and enter the bot token, numeric owner ID, authorized
numeric user IDs (including the owner), and optional negative group IDs.
The token input is masked. Group IDs do not authorize their members; the bot's
individual allowlist remains required. Advanced or noninteractive setup can use
a private file:

```bash
chmod 600 /root/mikachan-install.env
sudo bash deploy/manage.sh install --config /root/mikachan-install.env
```

Use `.env.example` as the key reference. The manager overrides `DATA_DIR` with
the persistent state path. Never put the bot token in a shell argument or in
source control. The panel validates values against the selected release's
`Settings` before starting it.

## Layout and service

| Path | Purpose |
| --- | --- |
| `/opt/mika-nexus/repository.git` | Local bare mirror of existing `vN` branches |
| `/opt/mika-nexus/releases/<commit>` | Git worktree and locked bot environment |
| `/opt/mika-nexus/current` | Atomic symlink to active release |
| `/opt/mika-nexus/manifest.json` | Installed branch, exact commit and activation history |
| `/var/lib/mika-nexus` | Persistent SQLite database and job artifacts, owned by the bot user |
| `/var/backups/mika-nexus` | Root-only SQLite/config snapshots |
| `/etc/mika-nexus.env` | Runtime secrets, mode `0640`, owner root/group `mika-nexus` |
| `/etc/systemd/system/mika-nexus.service` | One hardened service, with a polling lock |

The code and its Python environment are separated from state and configuration.
The package and systemd service retain the existing `mika-nexus` runtime name;
the product remains Mikachan.

Install checks out a specific commit from GitHub's `v2`, runs `uv sync --locked
--extra dev`, the full pytest suite and Ruff as gates, records mypy's result,
then validates configuration. The current downloader changes have known mypy
errors; the panel reports them rather than silently calling that check clean.
Only then does it activate and start the service. An update stages and checks
the next release **before stopping the running service**. It stops the service,
uses SQLite's backup API to snapshot the database, switches the `current`
symlink atomically, then verifies that the service stays active. A failed
activation restores the prior code, database and settings. If the service was
intentionally stopped, update and rollback keep it stopped.

Snapshots include SQLite and `/etc/mika-nexus.env`. They do not duplicate
potentially large job media in `/var/lib/mika-nexus/jobs`. For a full filesystem
backup, stop the service and back up the entire state directory separately.
Restoring a prior SQLite snapshot discards later database changes. The panel
saves an undo snapshot before manual restore or rollback.

## Commands and version semantics

```bash
sudo bash deploy/manage.sh status
sudo bash deploy/manage.sh diagnostics --live
sudo bash deploy/manage.sh versions --refresh
sudo bash deploy/manage.sh update
sudo bash deploy/manage.sh switch v4
sudo bash deploy/manage.sh switch v4 --commit FULL_40_CHARACTER_SHA
sudo bash deploy/manage.sh rollback
sudo bash deploy/manage.sh backups
sudo bash deploy/manage.sh backup
sudo bash deploy/manage.sh restore SNAPSHOT_ID
sudo bash deploy/manage.sh prune
sudo bash deploy/manage.sh prune --apply
sudo bash deploy/manage.sh logs --lines 100
```

`update` fetches the newest commit on the **currently installed major branch**;
it works the same way on v2, v4, or another existing `vN`. It refuses an
unexpected remote branch rewrite. `switch` selects another existing version
branch or a full commit reachable from that branch. It does not create `v3` or
any other new product branch. `rollback` activates the preceding installed
release and its pre-activation SQLite/config snapshot, even without network.
`prune` previews old worktrees and `--apply` removes worktrees outside the three
most recently activated distinct commits; it retains the current and previous
release and **does not delete backups**.

`diagnostics --live` runs the local doctor, SQLite quick check, host checks, and
Telegram `getMe`. It does not start a second bot or consume provider APIs.
An active systemd service and passing offline checks do not prove every external
provider works. Run feature-specific live acceptance on the target network.

`config` masks secret fields; `set KEY` prompts for a new value without putting
secrets in shell history. Authorization setting changes synchronize the SQLite
allowlist and cancel pending jobs for revoked users or groups. The owner ID,
data path and local Bot API migration are excluded from this generic setting
editor. For the official local Bot API: obtain Telegram `API_ID` and `API_HASH`,
bind the official server to loopback, stop public polling, call Telegram Bot API
`logOut`, then change mode and verify. Never run two consumers with one token.

## Failure, legacy installation, and removal

- If an update fails before activation, the running release is untouched. If
  activation fails, the manager attempts to restore the previous release and
  snapshot. `recover` handles an interrupted activation journal after a crash.
- If first installation fails before a manifest is committed, `status` shows
  **incomplete install**. `abort-install` clears only that panel-created code and
  unit, retaining state/config/backups, so installation can be retried.
- If GitHub or PyPI is unreachable, new staging cannot finish. The current
  installed release and an existing rollback remain usable.
- If `status` finds a checkout or systemd unit outside the panel's managed
  layout, **installation refuses to overwrite it**. An older `/opt/mika-nexus`
  checkout requires a reviewed migration of its unit, environment file and
  SQLite state. Do not point the panel at it and assume it was panel-installed.
- `sudo bash deploy/manage.sh remove --yes` stops and removes only the managed
  service and code. It retains config, database and backups for reinstall.
  `sudo bash deploy/manage.sh remove --purge --yes` also deletes those retained
  paths after an additional typed confirmation. Other projects, users and
  system packages are never removed.

The older `deploy/setup-ubuntu.sh --install` remains a developer workspace
setup/test helper; it is not the systemd release manager.
