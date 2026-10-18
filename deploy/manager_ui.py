"""Optional Textual interface for the standalone Mikachan deployment engine."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, RichLog, Static

from deploy.manager import Manager, ManagerError


class Fields(ModalScreen[dict[str, str] | None]):
    DEFAULT_CSS = """
    Fields { align: center middle; background: #080b12 80%; }
    #dialog { width: 65; max-width: 95%; height: auto; max-height: 90%;
              background: #182030; border: round #8d9bb7; padding: 1 2; }
    #dialog-title { text-style: bold; color: #e6e9f4; margin-bottom: 1; }
    .field-label { color: #b6c3d8; margin-top: 1; }
    Input { margin-bottom: 1; }
    #dialog-buttons { height: 3; margin-top: 1; }
    #dialog-buttons Button { margin-right: 1; }
    """

    def __init__(self, title: str, fields: list[tuple[str, str, bool]],
                 guidance: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.fields = fields
        self.guidance = guidance

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Static(self.title_text, id="dialog-title")
            if self.guidance:
                yield Static(self.guidance)
            for name, label, secret in self.fields:
                yield Static(label, classes="field-label")
                yield Input(id=f"field-{name}", password=secret)
            with Horizontal(id="dialog-buttons"):
                yield Button("Continue", id="confirm", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "confirm":
            self.dismiss({name: self.query_one(f"#field-{name}", Input).value.strip()
                          for name, _, _ in self.fields})


class MikachanPanel(App[None]):
    TITLE = "MIKACHAN  /  SERVER CONTROL"
    SUB_TITLE = "One bot. One service. Versioned releases."
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh", "Refresh status")]
    CSS = """
    Screen { background: #10151f; color: #d7dfeb; }
    Header { background: #202b3d; color: #f3f5fb; }
    #body { height: 1fr; }
    #sidebar { width: 31; min-width: 24; background: #172030;
               border-right: solid #3d4a61; padding: 1; }
    #sidebar-title { color: #f1f3f8; text-style: bold; margin-bottom: 1; }
    #sidebar Button { width: 100%; margin-bottom: 1; background: #26344c;
                      color: #edf1f7; border: none; }
    #sidebar Button:hover { background: #425b7c; }
    #main { padding: 1 2; }
    #status { height: 14; border: round #576a86; padding: 1 2;
              background: #172131; color: #dce6f2; }
    #activity { height: 3; margin-top: 1; color: #c3d2e8; }
    #log { height: 1fr; border: round #38475f; padding: 1;
           background: #111927; color: #cdd9e9; }
    Footer { background: #202b3d; }
    """

    ACTIONS = [
        ("Status", "status"), ("Preflight", "preflight"),
        ("Diagnostics + Telegram", "diagnostics"),
        ("Install OS tools", "deps"),
        ("Install v2", "install"), ("Refresh versions", "versions"),
        ("Clear failed setup", "abort-install"),
        ("Update current branch", "update"), ("Switch branch / commit", "switch"),
        ("Rollback code + data", "rollback"), ("Recover interruption", "recover"),
        ("Old release preview", "prune-preview"), ("Remove old releases", "prune"),
        ("Backup", "backup"), ("List backups", "backups"),
        ("Restore backup", "restore"), ("Show settings", "config"),
        ("Change setting", "set"), ("Start service", "start"),
        ("Stop service", "stop"), ("Restart service", "restart"),
        ("Recent logs", "logs"), ("Remove code", "remove"),
        ("Purge everything", "purge"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with VerticalScroll(id="sidebar"):
                yield Static("OPERATIONS", id="sidebar-title")
                for label, action in self.ACTIONS:
                    yield Button(label, id=f"action-{action}")
            with Vertical(id="main"):
                yield Static("Checking installation…", id="status")
                yield Static("Ready", id="activity")
                yield RichLog(id="log", highlight=True, markup=False, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self._launch("status")

    def action_refresh(self) -> None:
        self._launch("status")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.busy or not event.button.id or not event.button.id.startswith("action-"):
            return
        action = event.button.id.removeprefix("action-")
        forms: dict[str, tuple[str, list[tuple[str, str, bool]], str]] = {
            "install": ("Install Mikachan v2", [
                ("token", "Telegram bot token", True),
                ("owner", "Owner numeric Telegram ID", False),
                ("users", "Allowed user IDs, comma separated (include owner)", False),
                ("groups", "Allowed negative group IDs (optional)", False),
            ], "Requires git, uv, ffmpeg, Tesseract and systemd. Runs offline checks before activation."),
            "switch": ("Switch installed release", [
                ("branch", "Existing branch, for example v2", False),
                ("commit", "Full 40-character commit SHA (optional; blank = latest)", False),
            ], "A database snapshot is taken before service restart. Older code may need the old data."),
            "rollback": ("Restore previous release and data", [
                ("confirmation", "Type ROLLBACK", False),
            ], "Database changes made after the previous activation will be discarded."),
            "prune": ("Remove obsolete release worktrees", [
                ("confirmation", "Type PRUNE", False),
            ], "Keeps the three most recent distinct installed commits and every database backup."),
            "restore": ("Restore database and settings", [
                ("snapshot", "Snapshot ID (see List backups)", False),
                ("confirmation", "Type RESTORE", False),
            ], "A new undo snapshot will be saved first."),
            "set": ("Change runtime setting", [
                ("key", "Setting name, for example ALLOWED_TELEGRAM_IDS", False),
                ("value", "New value (hidden while typing)", True),
            ], "Authorization edits also synchronize the persistent allowlist."),
            "remove": ("Remove managed code and service", [
                ("confirmation", "Type REMOVE MIKACHAN", False),
            ], "Configuration, database and backups are retained."),
            "purge": ("Permanently remove all managed data", [
                ("confirmation", "Type PURGE MIKACHAN", False),
            ], "Deletes managed code, service, database, settings and backups."),
            "abort-install": ("Clear incomplete installation", [
                ("confirmation", "Type ABORT INSTALL", False),
            ], "Only a panel-created install without a committed manifest can be cleared."),
        }
        if action in forms:
            title, fields, guidance = forms[action]
            self.push_screen(Fields(title, fields, guidance),
                             callback=lambda result: self._submitted(action, result))
        else:
            self._launch(action)

    def _submitted(self, action: str, fields: dict[str, str] | None) -> None:
        if fields is None:
            return
        expected = {"rollback": "ROLLBACK", "restore": "RESTORE",
                    "prune": "PRUNE", "remove": "REMOVE MIKACHAN",
                    "purge": "PURGE MIKACHAN", "abort-install": "ABORT INSTALL"}
        if action in expected and fields.get("confirmation") != expected[action]:
            self.query_one("#log", RichLog).write("Cancelled: confirmation text did not match.")
            return
        self._launch(action, fields)

    def _launch(self, action: str, fields: dict[str, str] | None = None) -> None:
        if not self.busy:
            self.run_worker(self._execute(action, fields or {}), exclusive=True)

    async def _execute(self, action: str, fields: dict[str, str]) -> None:
        self.busy = True
        self.query_one("#activity", Static).update(f"Working: {action}…")
        for button in self.query("#sidebar Button").results(Button):
            button.disabled = True
        log = self.query_one("#log", RichLog)
        log.write(f"› {action}")

        def progress(message: str) -> None:
            self.call_from_thread(log.write, message)

        manager = Manager(on_event=progress)

        def run() -> Any:
            if action == "status":
                return manager.status()
            if action == "preflight":
                return manager.preflight()
            if action == "diagnostics":
                return manager.diagnostics(live=True)
            if action == "deps":
                return manager.install_os_dependencies()
            if action == "versions":
                return manager.versions(refresh=True)
            if action == "install":
                config = {"BOT_TOKEN": fields.get("token", ""),
                          "OWNER_TELEGRAM_ID": fields.get("owner", ""),
                          "ALLOWED_TELEGRAM_IDS": fields.get("users", "") or fields.get("owner", ""),
                          "ALLOWED_GROUP_IDS": fields.get("groups", ""),
                          "PRIVATE_CHATS_ONLY": "false" if fields.get("groups") else "true"}
                return manager.install(config)
            if action == "abort-install":
                return manager.abort_incomplete_install()
            if action == "update":
                return manager.update()
            if action == "switch":
                return manager.switch(fields.get("branch", ""),
                                      commit=fields.get("commit") or None)
            if action == "rollback":
                return manager.rollback()
            if action == "prune-preview":
                return manager.prune_releases()
            if action == "prune":
                return manager.prune_releases(apply=True)
            if action == "recover":
                return manager.recover()
            if action == "backup":
                return manager.backup()
            if action == "backups":
                return manager.backups()
            if action == "restore":
                return manager.restore(fields.get("snapshot", ""))
            if action == "config":
                return manager.config()
            if action == "set":
                return manager.set_config(fields.get("key", "").upper(), fields.get("value", ""))
            if action in {"start", "stop", "restart"}:
                return manager.service(action)
            if action == "logs":
                return manager.logs()
            if action == "remove":
                return manager.remove()
            if action == "purge":
                return manager.remove(purge=True)
            raise ManagerError("Unknown panel action")

        try:
            result = await asyncio.to_thread(run)
            if action == "status":
                self._show_status(result)
            else:
                log.write(json.dumps(result, indent=2, ensure_ascii=False)
                          if isinstance(result, (dict, list)) else str(result))
                if action != "logs":
                    self._show_status(await asyncio.to_thread(manager.status))
            self.query_one("#activity", Static).update("Ready")
        except (ManagerError, OSError, ValueError) as exc:
            log.write(f"Error: {exc}")
            self.query_one("#activity", Static).update("Action failed. Read the log below.")
        finally:
            self.busy = False
            for button in self.query("#sidebar Button").results(Button):
                button.disabled = False

    def _show_status(self, data: dict[str, Any]) -> None:
        if data.get("installed"):
            latest = data.get("known_remote_commit")
            available = ("up to date" if latest == data["commit"] else "newer or different commit known")
            quality = data.get("quality") or {}
            summary = ("MIKACHAN  /  INSTALLED\n"
                       f"Release      {data['branch']} @ {data['commit'][:12]}\n"
                       f"Service      {data['service']}\n"
                       f"Git state    {available}\n"
                       f"SQLite       {'present' if data['database'] else 'missing'}\n"
                       f"Access       {data.get('authorized_users', '?')} users / "
                       f"{data.get('authorized_groups', '?')} groups\n"
                       f"Consistency  {'OK' if data['consistent'] else 'MISMATCH'}\n"
                       f"Recovery     {'REQUIRED' if data['interrupted'] else 'clear'}\n"
                       f"Checks       pytest {quality.get('pytest', '?')} / "
                       f"Ruff {quality.get('ruff', '?')} / mypy {quality.get('mypy', '?')}\n"
                       "Live Telegram and provider access require separate acceptance.")
        else:
            summary = ("MIKACHAN  /  INCOMPLETE INSTALL\n" if data.get("incomplete_install")
                       else "MIKACHAN  /  NOT INSTALLED\n") + (
                       f"Service      {data['service']}\n"
                       f"Legacy files {'detected' if data.get('legacy_detected') else 'none detected'}\n"
                       "Run Preflight, then Install v2.\n"
                       "Existing unmanaged deployments require manual migration.")
        self.query_one("#status", Static).update(summary)


if __name__ == "__main__":
    MikachanPanel().run()
