# Terminal panel design review — 2026-10-18

The panel is an operator interface for one Mikachan installation. The primary tasks are confirming health, updating an existing `vN` branch, safely switching releases, controlling one polling service, changing numeric access, and protecting SQLite state. A second application server, dashboard daemon, or separate database would add idle resource use without improving those tasks.

## GitHub interface research

GitHub repositories and their documentation were reviewed on 2026-10-18. Star counts are approximate observations from that date, not quality scores.

| Project | Approx. stars | Relevant pattern | Mikachan decision |
| --- | ---: | --- | --- |
| [btop](https://github.com/aristocratos/btop) | 35k | Clear resource hierarchy, compact CPU/memory panels, strong contrast | Put load, memory, disk and service state above controls; avoid a wall of raw JSON on the landing screen. |
| [k9s](https://github.com/derailed/k9s) | 35k | Contextual command groups, navigable views, status while acting | Six stable sections and number-key navigation; destructive actions stay in explicit confirmation forms. |
| [lazygit](https://github.com/jesseduffield/lazygit) | 83k | Focused pane navigation and action-oriented workflow | Keep versions, logs and action feedback near the current section. |
| [lazydocker](https://github.com/jesseduffield/lazydocker) | 53k | Overview first, drill into service/process detail | A quick health card precedes detailed queue, storage and logs. |
| [Gum](https://github.com/charmbracelet/gum) | 24k | Short, guided prompts and deliberate confirmation | Setup asks only token and owner as required fields; optional access fields can be skipped. |
| [Textual](https://github.com/Textualize/textual) | 37k | Widget/CSS layout, keyboard support, modal forms, test pilot | Reuse the existing Python TUI and keep the dependency-free manager CLI functional if the UI environment fails. |

No code or assets were copied from these projects. The design uses a restrained dark navy palette with teal focus, a numbered rail, one health card, contextual action list and short activity log. Button labels say what happens. The six requested sections are Overview, Deployment, Service, Configuration, Data & Storage, and Maintenance. A terminal 110×36 navigation smoke test covers each section.

## Button scope and operational rules

| Action | Safeguard or data boundary |
| --- | --- |
| Update active branch | Fetch `vN`, stage a locked release, run checks, snapshot SQLite/config, then activate. No branch-number assumption. |
| Switch version/commit | Accept only an existing remote `vN` branch and a commit reachable from it. |
| Rollback/restore | Require typed confirmation; restore database together with code/config when appropriate. |
| Queue activity | Read SQLite metadata only; never print user IDs or submitted URLs. |
| Storage overview | Report sizes under the managed root only; show symlinks as needing inspection. |
| Clear interrupted media | Require stopped bot and free polling lock; remove only `state/jobs` and `tmp/runtime` entries. |
| Remove code / delete project | Separate actions and confirmation phrases. Full deletion needs an external backup if data must survive. |
| Configuration | Secrets are masked; numeric IDs and Bot API mode are validated before restart. |

All Mikachan-owned files are under `/opt/Mikachan`. The command launcher and systemd unit are the only project-specific paths outside it because the OS needs to discover them. The system user, journal and apt packages are platform-managed. Runtime temp, panel temp and bootstrap temp have separate subdirectories, so cleanup cannot delete an in-progress install or a live panel session. The downloader engine remains untouched; its existing `finally` cleanup is supplemented by clearing stale artifacts at the next start.

## Verification and current limits

The panel is tested with a disposable fake systemd/Git deployment, transient cleanup tests, and Textual pilot navigation. A successful local test cannot verify systemd permissions on every host or live Telegram delivery. The installer refuses the old split layout rather than silently migrating it. MyPy still reports errors in downloader-owned `bot.py`/`follow.py`; pytest and Ruff are the release gates, and mypy remains visible as advisory evidence.
