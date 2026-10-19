# Install and manage Mikachan on Ubuntu or Debian

This guide covers a **systemd** host. Use an interactive SSH session with sudo. Have at least 2 GB of free space, internet access to GitHub/PyPI, and the BotFather token and numeric owner Telegram ID ready. The default public Telegram Bot API needs **neither** Telegram API ID nor API hash.

## Public repository: one command

```bash
curl -fsSL https://raw.githubusercontent.com/Dneeel/Mikachan/v2/deploy/bootstrap.sh | sudo bash
```

Read the [bootstrap script](../deploy/bootstrap.sh) before running it if you prefer. It installs Git, Python venv support, FFmpeg, Tesseract with Persian language data, and Cairo; copies the panel to a stable location; retrieves the locked `v2` release; runs pytest and Ruff; creates a disabled service; and opens the panel. Setup then collects the two required Telegram values and starts the service. Installation stops if systemd is unavailable, an unmanaged service is present, or an existing service has the new managed unit name.

The first installation can take several minutes because Python packages and media tools must be fetched. The panel reports progress and errors. Run `mikachan` to reopen it.

## Private repository: read-only deploy key

Keep the key, SSH configuration and known-hosts file under the single Mikachan root. Check that `/opt/Mikachan` is absent or contains only files you intentionally prepared for Mikachan. Create a dedicated read-only deploy key:

```bash
sudo install -d -m 0700 /opt/Mikachan/config/ssh /opt/Mikachan/tmp/bootstrap
sudo ssh-keygen -t ed25519 -f /opt/Mikachan/config/ssh/deploy-key -N '' -C mikachan-deploy
sudo chmod 0600 /opt/Mikachan/config/ssh/deploy-key
sudo cat /opt/Mikachan/config/ssh/deploy-key.pub
```

Copy that public line into GitHub. Verify GitHub's published SSH fingerprint, then place its host key in `/opt/Mikachan/config/ssh/known_hosts` with mode `0600`. Create `/opt/Mikachan/config/ssh/config` with mode `0600`:

```sshconfig
Host github-mikachan
    HostName github.com
    User git
    IdentityFile /opt/Mikachan/config/ssh/deploy-key
    UserKnownHostsFile /opt/Mikachan/config/ssh/known_hosts
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

Test the deploy key with `sudo ssh -F /opt/Mikachan/config/ssh/config -T git@github-mikachan`. A GitHub success response may still exit nonzero because shell access is disabled.

Now install from the private repository in **one shell line** (after key setup):

```bash
sudo bash -c 'set -e; d=$(mktemp -d /opt/Mikachan/tmp/bootstrap/source.XXXXXX); trap '\''rm -rf "$d"'\'' EXIT; export GIT_SSH_COMMAND="ssh -F /opt/Mikachan/config/ssh/config"; git clone --depth 1 --branch v2 git@github-mikachan:Dneeel/Mikachan.git "$d/repo"; bash "$d/repo/deploy/bootstrap.sh" --source "$d/repo" --repo-url git@github-mikachan:Dneeel/Mikachan.git'
```

The panel saves the SSH Git remote and reuses that SSH configuration for later updates. Keep the deploy key while the server needs new releases. Never put it in `.env`, a Git remote URL, command history, or a shared backup. Revoking the key stops future fetches; it does not delete the installed release.

## First setup

1. Select **Guided setup**. Enter the BotFather token. It identifies the bot and is hidden while typing.
2. Enter the **numeric owner Telegram user ID**. This grants global owner controls; names and usernames are not accepted.
3. Optionally enter additional numeric user IDs, comma separated. Press Enter for owner-only access. This legacy setup seed accepts up to four IDs; the Owner can register more named people through private `/settings` later.
4. Optionally enter negative numeric group IDs, comma separated. Press Enter for private chats only. Group membership never authorizes a user automatically.
5. Optionally enter the numeric **Telegram API ID** from `my.telegram.org` for a real user account. This is separate from the bot token.
6. Enter the matching **Telegram API Hash** in the masked field. Leave both API fields blank if the user account is not ready yet.

The manager validates the complete configuration, saves a SQLite/configuration snapshot, and starts the service. It reports a service-start failure without claiming Telegram live acceptance. Use **Live diagnostics** to check `getMe` separately. Re-entering Setup displays a warning and requires typed confirmation; it preserves database content and saves an undo snapshot. Use Settings for a single change instead.

The API fields also appear near the top of **Configuration → Telegram user account · userbot**. After entering both fields, run `mikachan userbot-login` in a private SSH terminal to create the account session, then save it as masked `USERBOT_SESSION`. Configure `USERBOT_STAGING_CHAT_ID` there when large uploads or `/url` are needed. Do not paste a session or API hash into a Telegram chat.

The server's panel is a separate bootstrap copy from the active bot release. The first update from an older panel needs a one-time panel refresh: update the `v2` release, then install its `deploy/manager.py` and `deploy/manager_ui.py` into `/opt/Mikachan/panel/deploy/` using `install -m 0644`. Subsequent release activations refresh the bootstrap-owned panel automatically.

After startup, the protected Owner opens `/settings` in their private bot conversation to register people with required internal names, approve groups, assign Mikachan Admins to specific groups and set global module policy. ADMIN and USER may interact only in approved groups, even when Telegram has delivered them an outbound direct notice. Native Telegram administrator status alone does not grant Mikachan permissions. Editing the legacy allowlist/group ID fields in the VPS panel applies only the explicit difference from their previous values and preserves other people/groups registered inside the bot. Changing the primary Owner through the authenticated VPS panel updates the protected Owner registry with a snapshot.

A protected primary Owner may request promotion or removal of an additional Owner in the private `/settings` registry. The request cannot approve itself inside Telegram. On the VPS, inspect **Configuration → Pending Owner requests** and use **Approve Owner request**, or run `sudo mikachan owner-approval list` followed by `sudo mikachan owner-approval approve TOKEN USER_ID`. The latter asks for a typed confirmation. Approval stops the service, saves a consistent database snapshot, checks the request and target again, applies the change and restarts the service. Never paste a bot token or user-session secret into these commands.

## Everyday controls

Run `mikachan` from a server terminal. Number keys `1`–`6` select sections, `r` refreshes, and `q` exits.

| Section | Actions |
| --- | --- |
| 01 Overview | Dashboard with exact branch/commit, health, host load, memory, free disk, access counts; Guided setup; live diagnostics |
| 02 Deployment | Install/reinstall `v2` (reusing validated saved settings after a code-only removal), list available `vN` versions, update the active branch (including new commits on `v4`), switch branch or exact reachable commit, rollback code/data, preview and prune old releases |
| 03 Service | Start, stop, restart, inspect queue counts/recent states without user URLs, and read recent logs |
| 04 Configuration | View configuration with secrets masked; edit token, owner, allowlists, group IDs, optional providers and runtime limits individually |
| 05 Data & Storage | Inspect root usage; create, list and restore SQLite/config snapshots; clear interrupted media after stopping the bot |
| 06 Maintenance | Host checks, install missing tools, live diagnostics, recover interrupted activation, remove bot code, or delete all managed data |

A version switch takes a consistent SQLite snapshot after stopping the bot. A failed activation restores the previous code and data. A rollback intentionally restores the earlier snapshot and discards later database changes. Regular backups contain the SQLite database and runtime settings; large downloaded job artifacts are not duplicated. Review a target branch before switching to it, especially across major schema changes.

Every Mikachan-managed file lives under **`/opt/Mikachan`**:

| Path | Purpose |
| --- | --- |
| `/opt/Mikachan/panel`, `/opt/Mikachan/panel-venv` | Stable panel code and independent environment |
| `/opt/Mikachan/repository.git`, `/opt/Mikachan/releases/<commit>`, `/opt/Mikachan/current` | Git mirror, locked release environments and active symlink |
| `/opt/Mikachan/config/bot.env`, `/opt/Mikachan/config/manager.json`, `/opt/Mikachan/config/ssh` | Runtime settings, update remote and optional private deploy key |
| `/opt/Mikachan/state/mikachan.sqlite*`, `/opt/Mikachan/state/jobs` | SQLite WAL state and transient download artifacts |
| `/opt/Mikachan/cache`, `/opt/Mikachan/tmp`, `/opt/Mikachan/logs` | Dependency caches, isolated runtime/manager/panel/bootstrap temp and project logs |
| `/opt/Mikachan/backups` | Operator-created SQLite/configuration snapshots |
| `/opt/Mikachan/manifest.json`, `/opt/Mikachan/transaction.json` | Release history and interrupted-update recovery |

The only paths outside the root are the small `/usr/local/bin/mikachan` launcher, `/etc/systemd/system/mikachan.service`, and operating-system-managed user, journal and packages. The bot service runs as an unprivileged `mikachan` system user. The panel needs sudo because it manages that service, private settings, and backups. **Remove bot code** keeps data and backups. **Delete managed project** removes the entire managed root after typed confirmation, including the panel, local backups and secrets. Keep an external backup before purging.

QR, sticker, image and table conversions use bounded memory. Downloader files are removed after delivery or failure. A crash can leave a transient file until the next bot start; **Clear interrupted media** removes those leftovers while the service is stopped. SQLite, settings, code, caches and requested backups remain persistent.

## Noninteractive and development use

`mikachan status`, `mikachan preflight`, `mikachan queue`, `mikachan storage`, `mikachan diagnostics --live`, and `mikachan logs` work from the shell. `mikachan update` updates the active branch; `mikachan versions --refresh` lists remote major branches. Run `mikachan --help` for the complete CLI. Configuration values are never passed as CLI arguments by the panel. Avoid placing tokens directly in shell commands; use the masked panel input or a private `0600` config file.

Optional CoinPJ Toman quotes require a separate Telegram **user account** session. In Configuration, set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` for a Telegram app you control. Then run `cd /opt/Mikachan/current && ./.venv/bin/mikachan userbot-login` in your private SSH terminal. Complete the account login there and paste the resulting session secret into the panel's masked `COINPJ_SESSION` setting. This session lets the user account message `@CoinPJbot`; it is independent of the bot token and is never requested in a Telegram chat. A user session is not needed for the ordinary public Bot API. If the external bot is unavailable, Mikachan labels and attempts its public Toman estimate instead.

Optional large uploads and `/url` require a private staging channel or group with both the Telegram user account and Mikachan bot as members; the bot must be allowed to post, copy and delete messages there. Enter its negative numeric ID as `USERBOT_STAGING_CHAT_ID` and the session from `mikachan userbot-login` as masked `USERBOT_SESSION` (or reuse an existing `COINPJ_SESSION`). `/url` explicitly sends the selected media to `@FileToLinkiBot`, which may retain the file and advertises links that expire after 24 hours. Large download delivery uses Telegram's server-side copy from the staging chat and cannot supply a reusable Bot API file ID for the copied message. If staging access or the external bot fails, those actions report an error; ordinary downloads keep the Bot API path.

For a development checkout, run `uv sync --locked --extra dev`, `uv run pytest -q`, `uv run ruff check .`, and `uv run mikachan doctor`. A local poller must not run alongside the managed service for the same bot token.

## Existing installations

The one-line bootstrap refuses to overwrite an unmanaged or older managed service. Stop its poller, take a verified SQLite backup that includes WAL contents, preserve its configuration and job directory, and migrate paths and unit files during a planned maintenance window. Reusing a bot token in two simultaneous pollers can drop or duplicate updates. The automatic first-install flow does not claim to migrate an existing state directory.
