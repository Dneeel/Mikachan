# Install and manage Mikachan on Ubuntu or Debian

This guide covers a **systemd** host. Use an interactive SSH session with sudo. Have at least 2 GB of free space, internet access to GitHub/PyPI, and the BotFather token and numeric owner Telegram ID ready. The default public Telegram Bot API needs **neither** Telegram API ID nor API hash.

## Public repository: one command

```bash
curl -fsSL https://raw.githubusercontent.com/Dneeel/Mikachan/v2/deploy/bootstrap.sh | sudo bash
```

Read the [bootstrap script](../deploy/bootstrap.sh) before running it if you prefer. It installs Git, Python venv support, FFmpeg, Tesseract with Persian language data, and Cairo; copies the panel to a stable location; retrieves the locked `v2` release; runs pytest and Ruff; creates a disabled service; and opens the panel. Setup then collects the two required Telegram values and starts the service. Installation stops if systemd is unavailable, an unmanaged service is present, or an existing service has the new managed unit name.

The first installation can take several minutes because Python packages and media tools must be fetched. The panel reports progress and errors. Run `mikachan` to reopen it.

## Private repository: read-only deploy key

Create an SSH deploy key dedicated to this server. Add **only its public half** in GitHub repository **Settings → Deploy keys**, with write access **off**. Keep the private key on the server with mode `0600`. For example:

```bash
sudo install -d -m 0700 /root/.ssh
sudo ssh-keygen -t ed25519 -f /root/.ssh/mikachan-deploy -N '' -C mikachan-deploy
sudo chmod 0600 /root/.ssh/mikachan-deploy
sudo cat /root/.ssh/mikachan-deploy.pub
```

Copy that public line into GitHub. Configure a dedicated SSH host alias in `/root/.ssh/config` so other GitHub credentials stay separate:

```sshconfig
Host github-mikachan
    HostName github.com
    User git
    IdentityFile /root/.ssh/mikachan-deploy
    IdentitiesOnly yes
```

Set that config to mode `0600`. Verify GitHub's SSH host fingerprint against its published fingerprint before accepting the first connection, then test the deploy key with `sudo ssh -T git@github-mikachan`. A GitHub success response may still exit nonzero because shell access is disabled.

Now install from the private repository in **one shell line** (after key setup):

```bash
sudo bash -c 'd=$(mktemp -d); trap '\''rm -rf "$d"'\'' EXIT; git clone --depth 1 --branch v2 git@github-mikachan:Dneeel/Mikachan.git "$d/repo" && bash "$d/repo/deploy/bootstrap.sh" --source "$d/repo" --repo-url git@github-mikachan:Dneeel/Mikachan.git'
```

The panel saves the SSH Git remote for later updates. Keep the deploy key in place while this server needs to fetch updates. Never put it in `.env`, a Git remote URL, command history, or a backup shared with others. Revoking the key stops future fetches; it does not delete the installed release.

## First setup

1. Select **Guided setup**. Enter the BotFather token. It identifies the bot and is hidden while typing.
2. Enter the **numeric owner Telegram user ID**. This grants global owner controls; names and usernames are not accepted.
3. Optionally enter additional numeric user IDs, comma separated. Press Enter for owner-only access. Maximum total: four.
4. Optionally enter negative numeric group IDs, comma separated. Press Enter for private chats only. Group membership never authorizes a user automatically.

The manager validates the complete configuration, saves a SQLite/configuration snapshot, and starts the service. It reports a service-start failure without claiming Telegram live acceptance. Use **Live diagnostics** to check `getMe` separately. Re-entering Setup displays a warning and requires typed confirmation; it preserves database content and saves an undo snapshot. Use Settings for a single change instead.

## Everyday controls

Run `mikachan` from a server terminal. The panel has these sections:

| Section | Actions |
| --- | --- |
| Overview | Installed branch, exact commit, service, setup, access, database, quality checks; Guided setup; live diagnostics |
| Settings | View every supported configuration key with secrets masked; edit one key, including token, owner, allowlists, group IDs, and optional providers |
| Versions | Refresh available `vN` branches, update current branch (including later commits on `v4` or another branch), switch branch or exact reachable commit, rollback, prune old code |
| Backups | Save, list, and restore SQLite/configuration snapshots |
| Service | Start, stop, restart, and inspect recent logs |
| System | Host checks, install missing tools, recover interrupted activation, remove code, or purge managed data |

A version switch takes a consistent SQLite snapshot after stopping the bot. A failed activation restores the previous code and data. A rollback intentionally restores the earlier snapshot and discards later database changes. Regular backups contain the SQLite database and runtime settings; large downloaded job artifacts are not duplicated. Review a target branch before switching to it, especially across major schema changes.

The panel stores:

| Path | Purpose |
| --- | --- |
| `/usr/local/bin/mikachan` | Terminal command |
| `/usr/local/lib/mikachan-panel` | Stable panel code; independent of active bot release |
| `/opt/mikachan/repository.git` | Local Git mirror |
| `/opt/mikachan/releases/<commit>` | Locked Python environment for each release |
| `/opt/mikachan/current` | Symlink to active release |
| `/opt/mikachan/manifest.json` | Branch, commit, and activation history |
| `/var/lib/mikachan` | SQLite database and job data |
| `/var/backups/mikachan` | Manager snapshots |
| `/etc/mikachan.env` | Bot settings and secrets, root-owned, group-readable by bot only |
| `/etc/mikachan-manager.json` | Git remote used for updates |
| `/etc/systemd/system/mikachan.service` | Single bot service and polling lock |

The bot service runs as an unprivileged `mikachan` system user. The panel needs sudo because it manages that service, private settings, and backups. **Remove bot code** keeps data and backups. **Delete all managed data** removes code, configuration, SQLite state, and backups after typed confirmation. Keep external backups before purging. A full purge also removes the bootstrap-installed panel launcher, panel environment, and manager remote configuration. Install again with the one-line bootstrap if needed.

## Noninteractive and development use

`mikachan status`, `mikachan preflight`, `mikachan diagnostics --live`, and `mikachan logs` work from the shell. `mikachan update` updates the active branch; `mikachan versions --refresh` lists remote major branches. Run `mikachan --help` for the complete CLI. Configuration values are never passed as CLI arguments by the panel. Avoid placing tokens directly in shell commands; use the masked panel input or a private `0600` config file.

For a development checkout, run `uv sync --locked --extra dev`, `uv run pytest -q`, `uv run ruff check .`, and `uv run mikachan doctor`. A local poller must not run alongside the managed service for the same bot token.

## Existing installations

The one-line bootstrap refuses to overwrite an unmanaged or older managed service. Stop its poller, take a verified SQLite backup that includes WAL contents, preserve its configuration and job directory, and migrate paths and unit files during a planned maintenance window. Reusing a bot token in two simultaneous pollers can drop or duplicate updates. The automatic first-install flow does not claim to migrate an existing state directory.
