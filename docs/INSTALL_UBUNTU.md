# Install and manage Mikachan on Ubuntu or Debian

This guide covers a **systemd** host. Use an interactive SSH session with sudo. Have at least 2 GB of free space, internet access to GitHub/PyPI, and the BotFather token and numeric owner Telegram ID ready. The default public Telegram Bot API needs **neither** Telegram API ID nor API hash.

## Experimental version-aware installer (`v2-test` only)

The version-aware installer is currently available only on the `v2-test` branch.
On a **fresh VPS**, run the interactive selector:

```bash
curl -fsSL https://raw.githubusercontent.com/Dneeel/Mikachan/v2-test/deploy/bootstrap.sh | sudo bash
```

For a direct `v2-test` installation, use:

```bash
curl -fsSL https://raw.githubusercontent.com/Dneeel/Mikachan/v2-test/deploy/bootstrap-test.sh | sudo bash
```

The installer refuses to replace any existing Mikachan installation. Without a private
`--config` file, it stages the selected release and opens Guided setup; Telegram
service activation occurs only after valid configuration. See
[the Persian version-selection guide](INSTALL_VERSION_SELECTION_FA.md).

## Public repository: one command

```bash
curl -fsSL https://raw.githubusercontent.com/Dneeel/Mikachan/v2/deploy/bootstrap.sh | sudo bash
```

Read the [bootstrap script](../deploy/bootstrap.sh) before running it if you prefer. It installs Git, Python venv support, FFmpeg, Tesseract with Persian language data, and Cairo; copies the panel to a stable location; retrieves the locked `v2` release; runs pytest and Ruff; creates a disabled service; and opens the panel. Setup then collects the two required Telegram values and starts the service. Installation stops if systemd is unavailable, an unmanaged service is present, or an existing service has the new managed unit name.

The first installation can take several minutes because Python packages and media tools must be fetched. The panel reports progress and errors. Run `mikachan` to reopen it. The console panel uses the Python standard-library `curses` interface (no Textual). Optional owner-supplied artwork appears at full size when the terminal has enough room; a compact Mikachan title appears on smaller screens.

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

The API fields also appear near the top of **Configuration → Telegram user account · userbot**. After entering both fields, run `cd /opt/Mikachan/current && sudo ./.venv/bin/mikachan userbot-login` in a private interactive SSH terminal. Enter the same API ID and hash, then scan the displayed QR code in Telegram on your phone via **Settings → Devices → Link Desktop Device**. If your account uses two-step verification, enter that password in the terminal. On a managed VPS, this root command saves `USERBOT_SESSION` through the deployment manager, takes a configuration snapshot, and restarts the service. An unprivileged or unmanaged run prints the secret session for manual entry into the panel; scanning the QR alone does not configure the running bot. Configure `USERBOT_STAGING_CHAT_ID` there when large uploads or `/url` are needed. Do not paste a session or API hash into a Telegram chat. The QR code expires and refreshes automatically; run the command again if all five attempts expire.

The server's panel is a separate bootstrap copy from the active bot release. A verified release activation refreshes the bootstrap-owned panel. During the first upgrade from an older panel, newly introduced UI helpers are loaded from the verified active release until the panel copy is refreshed; appearance changes do not restart the bot.

After startup, the protected Owner opens `/settings` in their private bot conversation to register people with required internal names, approve groups, assign Mikachan Admins to specific groups and set global module policy. ADMIN and USER may interact only in approved groups, even when Telegram has delivered them an outbound direct notice. Native Telegram administrator status alone does not grant Mikachan permissions. Editing the legacy allowlist/group ID fields in the VPS panel applies only the explicit difference from their previous values and preserves other people/groups registered inside the bot. Changing the primary Owner through the authenticated VPS panel updates the protected Owner registry with a snapshot.

A protected primary Owner may request promotion or removal of an additional Owner in the private `/settings` registry. The request cannot approve itself inside Telegram. On the VPS, inspect **Configuration → Review pending co-owner requests**, select the desired request and confirm it, or run `sudo mikachan owner-approval list` followed by `sudo mikachan owner-approval approve TOKEN USER_ID`. The latter asks for a typed confirmation. Approval stops the service, saves a consistent database snapshot, checks the request and target again, applies the change and restarts the service. Never paste a bot token or user-session secret into these commands.

## Everyday controls

For MikaSub, use **Configuration → MikaSub** to set the optional
`MIKASUB_WORKER_URL` and `MIKASUB_ADMIN_TOKEN` after deploying the existing
Cloudflare Worker. The Admin token is masked and separate from the
Cloudflare deployment API token. An already created personal link may be
imported via masked `MIKASUB_PUBLIC_URL`. MikaSub needs no Telegram user API ID/hash.
The [MikaSub guide](MIKASUB.md) gives the Worker migration, binding and
Owner Telegram source-management steps. If these values are unset, the
rest of Mikachan can still run; `/vpn` reports that publication is not ready.

Run `mikachan` from an interactive SSH terminal. The interface plays a skippable startup animation, then opens a full-screen menu. **Space/Enter** skips the intro; **Up/Down** moves the selection; **Enter** opens it or submits a field; **Esc/Left** goes back; **Q** quits the main screen; **R** refreshes server status. Form fields use Enter only for the current field; setup automatically advances to the next field. Backspace edits; Esc cancels. Exiting the panel leaves the bot service running.

| Menu | Actions |
| --- | --- |
| Overview | Service/version/health summary, host resources, queue, diagnostics and storage |
| Services | Start, stop and restart the bot; inspect queue and integrated userbot configuration |
| Deployment | Browse version branches, install/update/switch, rollback, preview/prune releases |
| Configuration | Guided setup, masked setting picker, pending co-owner approvals |
| Integrations | Userbot, translation/data and music configuration; no separate userbot daemon |
| Logs | Scroll recent journal entries, show recent warnings/errors, filter literal text |
| Data & Backups | Create, browse and restore snapshots; inspect storage and recovery |
| Appearance | Intro mode/skip, artwork preview/reload/default, layout and alignment |
| Maintenance | Host preflight, dependencies, recovery, cleanup, remove and purge |
| Exit | Close the panel without stopping the bot |

**Safety:** Every destructive command displays its consequences and requires its own exact typed phrase. In particular, **Rollback restores a historical database as well as the code**; it can permanently discard changes recorded since that snapshot. Do not use rollback merely to change releases. Backups are SQLite/config snapshots, **not** a complete archive of all project code, cache and temporary media. Secrets are never displayed as setting values.

Place optional artwork at `/opt/Mikachan/assets/terminal/artwork/header.txt` and an optional MP4 or GIF at `/opt/Mikachan/assets/terminal/intro/intro.mp4` or `intro.gif`. The panel reads them on the next launch, or immediately after **Appearance → Reload**. The built-in text intro and compact header work without uploads. Terminal media is silent monochrome rendering; terminals smaller than 80×24, without color, or without FFmpeg use the native text intro. See the [asset guide](../assets/terminal/README.md) for limits and preview steps. There is no web server, graphical dashboard, long-running panel daemon or continuous idle CPU/RAM requirement. Installation/update work still runs only after a user selects it, using the existing verified Manager backend.

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
| `/opt/Mikachan/assets/terminal` | Persistent intro, artwork and appearance preferences |
| `/opt/Mikachan/manifest.json`, `/opt/Mikachan/transaction.json` | Release history and interrupted-update recovery |

The only paths outside the root are the small `/usr/local/bin/mikachan` launcher, `/etc/systemd/system/mikachan.service`, and operating-system-managed user, journal and packages. The bot service runs as an unprivileged `mikachan` system user. The panel needs sudo because it manages that service, private settings, and backups. **Remove bot code** keeps data and backups. **Delete managed project** removes the entire managed root after typed confirmation, including the panel, local backups and secrets. Keep an external backup before purging.

QR, sticker, image and table conversions use bounded memory. Downloader files are removed after delivery or failure. A crash can leave a transient file until the next bot start; **Clear interrupted media** removes those leftovers while the service is stopped. SQLite, settings, code, caches and requested backups remain persistent.

## Noninteractive and development use

`mikachan status`, `mikachan preflight`, `mikachan queue`, `mikachan storage`, `mikachan diagnostics --live`, and `mikachan logs` work from the shell. `mikachan update` updates the active branch; `mikachan versions --refresh` lists remote major branches. Run `mikachan --help` for the complete CLI. Configuration values are never passed as CLI arguments by the panel. Avoid placing tokens directly in shell commands; use the masked panel input or a private `0600` config file.

Optional CoinPJ Toman quotes use the same `USERBOT_SESSION` by default; set a separate masked `COINPJ_SESSION` only if a different Telegram account is needed. This session lets the user account message `@CoinPJbot`; it is independent of the bot token and is never requested in a Telegram chat. A user session is not needed for the ordinary public Bot API. If the external bot is unavailable, Mikachan labels and attempts its public Toman estimate instead.

Optional large uploads require a private staging channel or group with both the Telegram user account and Mikachan bot as members; the bot must be allowed to post, copy and delete messages there. Enter its negative numeric ID as `USERBOT_STAGING_CHAT_ID` and register `USERBOT_SESSION` as above. With a staging chat, `/url` copies the selected media there and forwards it to `@FileToLinkiBot` through the user account. Without staging, `/url` downloads a replied media item through the public Bot API (up to 20 MB) into a short-lived temporary file and sends that file through the user account. The third-party bot may retain the file and advertises links that expire after 24 hours. Large download delivery uses Telegram's server-side copy from the staging chat and cannot supply a reusable Bot API file ID for the copied message. If staging access or the external bot fails, those actions report an error; ordinary downloads keep the Bot API path.

For a development checkout, run `uv sync --locked --extra dev`, `uv run pytest -q`, `uv run ruff check .`, and `uv run mikachan doctor`. A local poller must not run alongside the managed service for the same bot token.

## Existing installations

The one-line bootstrap refuses to overwrite an unmanaged or older managed service. Stop its poller, take a verified SQLite backup that includes WAL contents, preserve its configuration and job directory, and migrate paths and unit files during a planned maintenance window. Reusing a bot token in two simultaneous pollers can drop or duplicate updates. The automatic first-install flow does not claim to migrate an existing state directory.
