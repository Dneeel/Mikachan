# Terminal management design review — 2026-10-18

Historical first-pass review. The revised six-section panel and single-root layout are documented in [Panel Design](PANEL_DESIGN_2026-10-18.md) and [Installation](INSTALL_UBUNTU.md). Paths and test counts below describe the earlier implementation, not the current installation.

## What this bot needs

The active v2 repository has one aiogram polling entrypoint, Python 3.12,
locked dependencies, SQLite in WAL mode, a service template, and a separate
Daytona foreground launcher. Its old Ubuntu helper creates `.venv` and runs
tests but does not own releases, rollback or uninstall. The existing systemd
template assumed a fixed checkout under `/opt/mikachan`; runtime state belongs
in `/var/lib/mikachan`. Downloader engines and Telegram event routing need no
changes for server management.

Required operations: install v2 from the actual v2 branch; report exact
branch/commit and service/config/database state; update the active branch even
when the major number is v4 or later; select a reachable older commit or a
different existing version branch; restore code and SQLite together; inspect
logs; edit guarded settings/allowlists; diagnose the host and Telegram identity;
remove code while retaining data, or explicitly purge it. No extra polling
process, web server, Docker daemon, or new database is needed.

## GitHub and documentation review

| Candidate/pattern | Finding | Decision |
| --- | --- | --- |
| [Textual](https://github.com/Textualize/textual), [guide](https://textual.textualize.io/guide/) | Python widgets, CSS, keyboard navigation, modal forms and a test pilot suit an operator TUI. MIT licensed. | Optional full-screen UI. Keep the deployment engine usable with Python's standard library when Textual is unavailable. |
| [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) | Excellent prompts/completion, but a persistent status/log/action screen needs more layout code. | Useful pattern, not the main UI. |
| [Gum](https://github.com/charmbracelet/gum) and [Bubble Tea](https://github.com/charmbracelet/bubbletea) | Good terminal UX. Gum adds an external shell-oriented binary; Bubble Tea requires a separate Go application for a Python bot. | Avoid a second implementation/runtime for this one-bot manager. |
| [Urwid](https://github.com/urwid/urwid), stdlib curses | Lower-level render and input handling increases custom code for forms and async progress. | Avoid for this release. |
| [Capistrano](https://github.com/capistrano/capistrano) | Proven `releases/`, shared state and `current` symlink pattern. | Apply that pattern, without bringing Ruby or Capistrano into the Python bot. |
| [Git worktrees](https://git-scm.com/docs/git-worktree), [fetch refspecs](https://git-scm.com/docs/git-fetch) | Detached worktrees preserve exact commits; explicit `vN` refspecs avoid accidentally using the repository's default `v1`. | Keep a local bare mirror and release worktrees; fast-forward check for same-branch updates. |
| [uv locked sync](https://docs.astral.sh/uv/concepts/projects/sync/) | `--locked` refuses a stale lock instead of silently changing dependency resolution. | Stage dependencies and run project checks before activation. |
| [SQLite Backup API](https://www.sqlite.org/backup.html) | A consistent SQLite snapshot can be taken even with WAL; stopping the service also prevents new bot writes during activation. | Back up SQLite and config on every activation, not a raw copy of only the main DB file. |
| [systemd service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html) | Supervision, startup order and process status are already available on a normal Ubuntu host. | One hardened system service with a shared `flock` path; no second polling consumer. |

The UI deliberately does not depend on the bot package. If a dependency
upgrade breaks the bot's `.venv`, the standalone manager can still show status,
restore a prior release, and report errors. A small CLI exposes the same
operations for SSH sessions too narrow for the full-screen UI and for scripted
checks. Textual and uv are pinned in a separate manager environment.

On the latest v2 checkout, 110 local tests and Ruff pass. Mypy reports 39
errors in downloader-owned `bot.py` and `follow.py`. The manager runs mypy and
records the failure in each release's quality record; pytest and Ruff are
activation gates. This keeps downloader ownership intact and does not label
the release type-clean.

## Scenario decisions and boundaries

| Scenario | Behavior |
| --- | --- |
| New server, default GitHub branch still v1 | Bootstrap explicitly clones v2; manager fetches `refs/heads/v*` and targets v2 by default. |
| New commit on installed v4 | `update` fast-forwards v4 to its newest reachable commit, stages/tests it, snapshots state, then swaps. |
| Previous/next major or specific revision | `switch vN [--commit SHA]` accepts only existing `vN` branches and commits in that remote branch's history. |
| Network outage during fetch or package sync | No service interruption occurs before staging/checks complete. |
| Corrupt/failed service after activation | Restore pre-activation SQLite/config and prior symlink; retain an interruption journal if recovery itself fails. |
| Operator intentionally stopped the bot | Update/config/restore preserve the stopped state. |
| Interrupted process or power failure | `status` flags journal/manifest mismatch; `recover` rolls back or finalizes a committed, healthy activation. |
| Unknown legacy checkout or unit | Refuse overwrite. Migration needs explicit inspection of real paths and backups. |
| Different local poller | `flock` prevents a second process from using the same state lock; manager checks it before switching/deletion. |
| Full uninstall | Require explicit removal; retain state by default, purge only with second typed confirmation. |
| Local Telegram Bot API mode | Keep migration manual because Telegram `logOut` and single-consumer sequencing are operational prerequisites. |

The status screen reports **service activity**, not blanket feature success.
`diagnostics --live` verifies `getMe`; external source availability must be
tested separately on the selected host. Database snapshots omit large transient
media artifacts. Backups are retained until an operator removes them; release
pruning never touches them. No unattended automatic upgrade is installed.
