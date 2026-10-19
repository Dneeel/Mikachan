"""Keyboard-friendly Mikachan server dashboard and guided first-run setup."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, RichLog, Static

from deploy.manager import EDITABLE, ENV_KEYS, PRIVATE_KEYS, Manager, ManagerError


SETTING_HELP = {
    "BOT_TOKEN": "Telegram BotFather token. Required to connect the bot. Changing it switches bot identity.",
    "OWNER_TELEGRAM_ID": "Your numeric Telegram user ID. Grants global owner controls.",
    "ALLOWED_TELEGRAM_IDS": "Legacy numeric seed IDs, separated by commas; Owner required, up to four here. Register further named people in private /settings.",
    "ALLOWED_GROUP_IDS": "Negative numeric group IDs, separated by commas. Members still need user access.",
    "PRIVATE_CHATS_ONLY": "Legacy setting retained for config compatibility. v2 permits only Owners privately; registered people may use approved groups.",
    "MOMMY_TELEGRAM_IDS": "Optional authorized numeric IDs for the separate fictional personality mode.",
    "BOT_API_MODE": "public is the default. local requires a separately installed Bot API server.",
    "LOCAL_BOT_API_URL": "Loopback URL for an explicitly configured local Bot API server.",
    "TELEGRAM_API_ID": "Numeric API ID from my.telegram.org for your Telegram user account.",
    "TELEGRAM_API_HASH": "Private API hash paired with that API ID. Never send it to the bot.",
    "COINPJ_SESSION": "Optional secret Telegram user session; create with the one-time userbot-login command.",
    "USERBOT_SESSION": "Optional secret user session for large uploads, /url and currency requests.",
    "USERBOT_STAGING_CHAT_ID": "Private channel/group ID accessible to both the bot and the user account. Never use a public chat.",
    "SPOTIFY_CLIENT_ID": "Optional Spotify catalog browsing credential.",
    "SPOTIFY_CLIENT_SECRET": "Optional Spotify catalog browsing secret.",
    "NASA_API_KEY": "Optional NASA APOD key; DEMO_KEY is used otherwise.",
    "NAVASAN_API_KEY": "Optional direct Iranian market feed credential.",
    "NAVASAN_REFRESH_SECONDS": "Market refresh interval; default 28800 seconds.",
    "TRANSLATION_API_URL": "Optional owner-chosen translation provider URL.",
    "TRANSLATION_API_KEY": "Optional translation provider secret.",
}

SECTIONS = {
    "overview": [("Refresh dashboard", "status"), ("Guided setup", "setup"),
                 ("Live diagnostics", "diagnostics")],
    "deployment": [("Install or reinstall v2", "install"), ("Available versions", "versions"),
                   ("Update active branch", "update"),
                   ("Switch version or commit", "switch"), ("Rollback code and data", "rollback"),
                   ("Preview release cleanup", "prune-preview"), ("Prune old releases", "prune")],
    "service": [("Start", "start"), ("Stop", "stop"), ("Restart", "restart"),
                ("Queue activity", "queue"), ("Recent logs", "logs")],
    "configuration": [("Refresh configuration", "config"),
                      ("Pending Owner requests", "owner-requests"),
                      ("Approve Owner request", "owner-approve")],
    "data": [("Storage overview", "storage"), ("Create backup", "backup"),
             ("List backups", "backups"), ("Restore backup", "restore"),
             ("Clear interrupted media", "cleanup")],
    "maintenance": [("Host preflight", "preflight"), ("Install host tools", "deps"),
                    ("Live diagnostics", "diagnostics"), ("Recover interrupted update", "recover"),
                    ("Remove bot code", "remove"), ("Delete managed project", "purge")],
}

SECTION_LABELS = {
    "overview": "01  Overview", "deployment": "02  Deployment",
    "service": "03  Service", "configuration": "04  Configuration",
    "data": "05  Data & Storage", "maintenance": "06  Maintenance",
}

SETTING_GROUPS = {
    "Identity and access": ("BOT_TOKEN", "OWNER_TELEGRAM_ID", "ALLOWED_TELEGRAM_IDS",
                            "ALLOWED_GROUP_IDS", "PRIVATE_CHATS_ONLY", "MOMMY_TELEGRAM_IDS"),
    "Telegram user account · userbot": ("TELEGRAM_API_ID", "TELEGRAM_API_HASH",
                                        "USERBOT_SESSION", "USERBOT_STAGING_CHAT_ID",
                                        "COINPJ_SESSION"),
    "Information and language": ("NASA_API_KEY", "NAVASAN_API_KEY",
                                 "NAVASAN_REFRESH_SECONDS", "TRANSLATION_API_URL",
                                 "TRANSLATION_API_KEY"),
    "Music catalog": ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"),
    "Limits": ("MAX_ACTIVE_DOWNLOADS", "MAX_ACTIVE_PER_USER", "MAX_LINKS_PER_MESSAGE",
               "SEARCH_PAGE_SIZE", "SEARCH_SESSION_TTL_SECONDS", "CONFIRMATION_TTL_SECONDS"),
    "Advanced Bot API": ("BOT_API_MODE", "LOCAL_BOT_API_URL"),
    "Network and storage": ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE",
                            "DATA_DIR", "FREESTYLE_API_KEY"),
}


class Fields(ModalScreen[dict[str, str] | None]):
    DEFAULT_CSS = """
    Fields { align: center middle; background: #080e19 85%; }
    #dialog { width: 70; max-width: 95%; height: auto; max-height: 90%;
              background: #1b293d; border: round #55d6c2; padding: 1 2; }
    #dialog-title { text-style: bold; color: #f3f6fb; margin-bottom: 1; }
    #guidance { color: #b9cee2; margin-bottom: 1; }
    .field-label { color: #d4e0ec; margin-top: 1; }
    Input { margin-bottom: 1; }
    #dialog-buttons { height: 3; margin-top: 1; }
    #dialog-buttons Button { margin-right: 1; }
    """

    def __init__(self, title: str, fields: list[tuple[str, str, bool]],
                 guidance: str = "", defaults: dict[str, str] | None = None) -> None:
        super().__init__()
        self.title_text = title
        self.fields = fields
        self.guidance = guidance
        self.defaults = defaults or {}

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Static(self.title_text, id="dialog-title")
            if self.guidance:
                yield Static(self.guidance, id="guidance")
            for name, label, secret in self.fields:
                yield Static(label, classes="field-label")
                yield Input(value=self.defaults.get(name, ""), id=f"field-{name}",
                            password=secret, placeholder="Optional — press Enter to skip" if
                            "optional" in label.lower() else "")
            with Horizontal(id="dialog-buttons"):
                yield Button("Continue", id="confirm", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        if self.fields:
            self.query_one(f"#field-{self.fields[0][0]}", Input).focus()

    def on_input_submitted(self, _: Input.Submitted) -> None:
        self.submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "confirm":
            self.submit()

    def submit(self) -> None:
        self.dismiss({name: self.query_one(f"#field-{name}", Input).value.strip()
                      for name, _, _ in self.fields})


class MikachanPanel(App[None]):
    TITLE = "MIKACHAN  /  CONTROL"
    SUB_TITLE = "One installation · one bot"
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh", "Refresh"),
                ("1", "section('overview')", "Overview"),
                ("2", "section('deployment')", "Deploy"),
                ("3", "section('service')", "Service"),
                ("4", "section('configuration')", "Config"),
                ("5", "section('data')", "Data"),
                ("6", "section('maintenance')", "Maintain")]
    CSS = """
    Screen { background: #0b111c; color: #e7edf5; }
    Header { background: #152334; color: #edf4fa; }
    #body { height: 1fr; }
    #nav { width: 27; min-width: 23; background: #111e2d; padding: 1;
           border-right: solid #28445a; }
    #brand { text-style: bold; color: #67dec5; margin-bottom: 1; }
    #nav-hint { color: #819db1; margin-bottom: 2; }
    #nav Button { width: 100%; margin-bottom: 1; background: #1a3044;
                  color: #d4e6ee; border: none; text-align: left; }
    #nav Button.-active { background: #225564; color: #f5fffc; text-style: bold; }
    #nav Button:hover { background: #2b6571; }
    #main { padding: 1 2; }
    #section-title { height: 2; text-style: bold; color: #67dec5; }
    #status { height: 10; border: round #345b6e; padding: 0 2;
              background: #15283b; color: #edf4fb; }
    #actions { height: 1fr; margin-top: 1; }
    #actions .action-row { height: 4; }
    #actions .action-row Button { width: 1fr; margin-right: 1; margin-bottom: 1;
                                  background: #203d52; color: #e7f1f7; border: none;
                                  content-align: left middle; }
    #actions Button { width: 100%; margin-bottom: 1;
                      background: #203d52; color: #e7f1f7; border: none;
                      content-align: left middle; }
    #actions Button:hover { background: #2e6570; }
    #actions .field-label { color: #67dec5; text-style: bold; margin-top: 1; }
    #activity { height: 1; color: #a5c8d6; }
    #log { height: 6; border: round #2c4d5e; padding: 0 1;
           background: #0e1b2a; color: #d4e2ee; }
    Footer { background: #152334; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.busy = False
        self.section = "overview"
        self.setup_data: dict[str, str] = {}
        self.setup_step = 0
        self.setup_existing = False
        self.current_config: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="nav"):
                yield Static("◈  MIKACHAN", id="brand")
                yield Static("SERVER CONTROL  ·  v2+", id="nav-hint")
                for key, label in SECTION_LABELS.items():
                    yield Button(label, id=f"nav-{key}")
            with Vertical(id="main"):
                yield Static("OVERVIEW", id="section-title")
                yield Static("Checking installation…", id="status")
                yield VerticalScroll(id="actions")
                yield Static("Ready", id="activity")
                yield RichLog(id="log", highlight=True, markup=False, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self.show_section("overview")
        self._launch("status")

    def action_refresh(self) -> None:
        self._launch("status")

    def action_section(self, section: str) -> None:
        if section in SECTIONS and not self.busy:
            self.show_section(section)

    def show_section(self, section: str, *, refresh: bool = True) -> None:
        self.section = section
        self.query_one("#section-title", Static).update(SECTION_LABELS[section].upper())
        for key in SECTIONS:
            self.query_one(f"#nav-{key}", Button).set_class(key == section, "-active")
        actions = self.query_one("#actions", VerticalScroll)
        actions.remove_children()
        section_actions = SECTIONS[section]
        for index in range(0, len(section_actions), 2):
            actions.mount(Horizontal(*(
                Button(label, id=f"action-{action}")
                for label, action in section_actions[index:index + 2]), classes="action-row"))
        if section == "configuration":
            for group, keys in SETTING_GROUPS.items():
                actions.mount(Static(group.upper(), classes="field-label"))
                for key in keys:
                    if key not in ENV_KEYS:
                        continue
                    value = self.current_config.get(key, "")
                    if key in EDITABLE:
                        actions.mount(Button(f"{key}   {value or '—'}", id=f"setting-{key}"))
                    else:
                        actions.mount(Static(f"{key}   {value or '—'}"))
            if refresh:
                self._launch("config")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.busy or not event.button.id:
            return
        if event.button.id.startswith("nav-"):
            self.show_section(event.button.id[4:])
            return
        if event.button.id.startswith("setting-"):
            self._setting_selected({"key": event.button.id.removeprefix("setting-")})
            return
        if not event.button.id.startswith("action-"):
            return
        action = event.button.id[7:]
        if action == "setup":
            self.begin_setup()
            return
        if action == "set":
            self.push_screen(Fields("Change one setting", [("key", "Setting name", False)],
                                    "Enter the name shown in View all settings."),
                             callback=self._setting_selected)
            return
        forms: dict[str, tuple[str, list[tuple[str, str, bool]], str]] = {
            "switch": ("Switch release", [("branch", "Major branch (for example v2)", False),
                        ("commit", "Optional full commit SHA", False)],
                       "A snapshot is saved before activation."),
            "rollback": ("Rollback code and data", [("confirmation", "Type ROLLBACK", False)],
                         "Restores the previous database snapshot. Later changes are lost."),
            "prune": ("Remove old releases", [("confirmation", "Type PRUNE", False)],
                      "Keeps at least the three most recent release commits."),
            "restore": ("Restore snapshot", [("snapshot", "Snapshot ID", False),
                        ("confirmation", "Type RESTORE", False)],
                       "An undo snapshot is saved first."),
            "owner-approve": ("Approve Owner request",
                [("token", "Pending request token", False),
                 ("target", "Target numeric user ID", False),
                 ("confirmation", "Type APPROVE OWNER", False)],
                "Independent VPS operator approval. A snapshot is saved before the role change."),
            "remove": ("Remove bot code", [("confirmation", "Type REMOVE MIKACHAN", False)],
                       "Keeps configuration, database and backups."),
            "purge": ("Delete managed project", [("confirmation", "Type PURGE MIKACHAN", False)],
                      "Deletes bot code, configuration, database and backups permanently."),
        }
        if action in forms:
            title, fields, guidance = forms[action]
            self.push_screen(Fields(title, fields, guidance),
                             callback=lambda result: self._submitted(action, result))
        else:
            self._launch(action)

    def begin_setup(self) -> None:
        manager = Manager()
        try:
            state = manager.status()
            if not state.get("installed"):
                self.query_one("#log", RichLog).write("Install a v2 release in Deployment first.")
                return
            self.setup_existing = state.get("config", False)
        except ManagerError as exc:
            self.query_one("#log", RichLog).write(f"Error: {exc}")
            return
        if self.setup_existing:
            self.push_screen(Fields("Run setup again?", [("confirmation", "Type SETUP", False)],
                                    "Changing bot identity or owner may interrupt existing access. "
                                    "Mikachan saves a database and configuration snapshot first. "
                                    "Your existing data is retained."),
                             callback=self._setup_confirmed)
        else:
            self._setup_confirmed({"confirmation": "SETUP"})

    def _setup_confirmed(self, result: dict[str, str] | None) -> None:
        if not result or result.get("confirmation") != "SETUP":
            return
        self.setup_data = {}
        self.setup_step = 0
        self._next_setup()

    def _next_setup(self) -> None:
        steps = [
            ("1 / 6  ·  Connect Telegram", "token", "Bot token", True,
             "Required. Create or select a bot with @BotFather. The token identifies this bot; "
             "it is stored in a private server file and hidden in this panel."),
            ("2 / 6  ·  Choose owner", "owner", "Owner numeric Telegram user ID", False,
             "Required. This numeric ID grants global owner controls. A username is not accepted."),
            ("3 / 6  ·  Allow users", "users", "Optional additional numeric user IDs", False,
             "Press Enter to keep access owner-only. Separate multiple IDs with commas; "
             "the owner is automatically included. Maximum four users total."),
            ("4 / 6  ·  Allow groups", "groups", "Optional negative group IDs", False,
             "Press Enter for private chats only. Every group member still needs individual access."),
            ("5 / 6  ·  Telegram user account", "api_id", "Optional Telegram API ID", False,
             "Get your numeric API ID at my.telegram.org. This is for the userbot account, "
             "not the BotFather token. Leave both API fields blank to skip userbot setup."),
            ("6 / 6  ·  Telegram user account", "api_hash", "Optional Telegram API Hash", True,
             "Paste the API Hash paired with your API ID. It is masked and stored privately. "
             "After setup, log in once from private SSH with: mikachan userbot-login."),
        ]
        if self.setup_step == len(steps):
            api_id = self.setup_data.get("api_id", "")
            api_hash = self.setup_data.get("api_hash", "")
            if bool(api_id) != bool(api_hash):
                self.query_one("#log", RichLog).write(
                    "Enter both Telegram API ID and Hash, or leave both blank.")
                self.setup_step = 4
                self._next_setup()
                return
            if api_id and (not api_id.isascii() or not api_id.isdigit() or int(api_id) <= 0):
                self.query_one("#log", RichLog).write("Telegram API ID must be a positive number.")
                self.setup_step = 4
                self._next_setup()
                return
            owner = self.setup_data["owner"]
            users = self.setup_data.get("users", "")
            settings = {"BOT_TOKEN": self.setup_data["token"],
                        "OWNER_TELEGRAM_ID": owner,
                        "ALLOWED_TELEGRAM_IDS": ",".join(filter(None, (owner, users))),
                        "ALLOWED_GROUP_IDS": self.setup_data.get("groups", "")}
            if api_id:
                settings.update({"TELEGRAM_API_ID": api_id, "TELEGRAM_API_HASH": api_hash})
            self._launch("setup", settings)
            self.setup_data = {}
            return
        title, name, label, secret, guidance = steps[self.setup_step]
        self.push_screen(Fields(title, [(name, label, secret)], guidance),
                         callback=lambda result: self._setup_answer(name, result))

    def _setup_answer(self, name: str, result: dict[str, str] | None) -> None:
        if result is None:
            self.setup_data = {}
            return
        if not result[name] and name in {"token", "owner"}:
            self.query_one("#log", RichLog).write("This step is required.")
            self._next_setup()
            return
        self.setup_data[name] = result[name]
        self.setup_step += 1
        self._next_setup()

    def _setting_selected(self, result: dict[str, str] | None) -> None:
        if not result:
            return
        key = result["key"].upper()
        if key not in EDITABLE:
            self.query_one("#log", RichLog).write("Unknown or managed-only setting. View all settings first.")
            return
        guidance = SETTING_HELP.get(key, "Optional runtime setting. Enter a new value to replace it.")
        self.push_screen(Fields(f"Change {key}", [("value", "New value", key in PRIVATE_KEYS)],
                                guidance, defaults={"value": "" if key in PRIVATE_KEYS else
                                                    self.current_config.get(key, "")}),
                         callback=lambda data: self._submitted("set", {"key": key,
                                                     "value": data["value"]} if data else None))

    def _submitted(self, action: str, fields: dict[str, str] | None) -> None:
        if fields is None:
            return
        expected = {"rollback": "ROLLBACK", "restore": "RESTORE", "prune": "PRUNE",
                    "remove": "REMOVE MIKACHAN", "purge": "PURGE MIKACHAN",
                    "owner-approve": "APPROVE OWNER"}
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
        for button in self.query("Button").results(Button):
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
                return manager.install(reuse_existing_config=True)
            if action == "setup":
                return manager.setup(fields)
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
            if action == "queue":
                return manager.queue()
            if action == "storage":
                return manager.storage()
            if action == "cleanup":
                return manager.cleanup_transient()
            if action == "restore":
                return manager.restore(fields.get("snapshot", ""))
            if action == "config":
                return manager.config()
            if action == "set":
                return manager.set_config(fields["key"], fields["value"])
            if action == "owner-requests":
                return manager.owner_approvals()
            if action == "owner-approve":
                try:
                    target_id = int(fields["target"])
                except ValueError as exc:
                    raise ManagerError("Target must be a numeric Telegram ID") from exc
                return manager.approve_owner(fields["token"], target_id)
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
                if action == "config" and isinstance(result, dict):
                    self.current_config = result
                    if self.section == "configuration":
                        self.show_section("configuration", refresh=False)
                log.write(json.dumps(result, indent=2, ensure_ascii=False) if
                          isinstance(result, (dict, list)) else str(result))
                if action != "logs":
                    self._show_status(await asyncio.to_thread(manager.status))
            self.query_one("#activity", Static).update("Ready")
        except (ManagerError, OSError, ValueError) as exc:
            log.write(f"Error: {exc}")
            self.query_one("#activity", Static).update("Action failed. See details below.")
        finally:
            self.busy = False
            for button in self.query("Button").results(Button):
                button.disabled = False
            if action == "set" and self.section == "configuration":
                self._launch("config")

    def _show_status(self, data: dict[str, Any]) -> None:
        host = data.get("host") or {}
        def size(value: Any) -> str:
            return f"{value / 1024 ** 3:.1f} GiB" if isinstance(value, int) else "—"
        if data.get("installed"):
            quality = data.get("quality") or {}
            queue = data.get("queue") or {}
            setup = data.get("setup_required", False)
            summary = ("●  MIKACHAN   " + ("SETUP NEEDED" if setup else data['service'].upper()) + "\n"
                       f"Release       {data['branch']}  ·  {data['commit'][:12]}  ·  "
                       f"{'healthy' if data['consistent'] else 'inspect'}\n"
                       f"Access        {data.get('authorized_users', 0)} users  /  "
                       f"{data.get('authorized_groups', 0)} groups\n"
                       f"Database      {'ready' if data['database'] else 'not created'}\n"
                       f"CPU           {host.get('cpu_percent', '—')}%  ·  "
                       f"load {host.get('load_1m', '—')}\n"
                       f"Memory        {size(host.get('memory_used'))} / "
                       f"{size(host.get('memory_total'))}\n"
                       f"Free disk     {size(host.get('disk_free'))}\n"
                       f"Activity      {queue.get('waiting', 0)} waiting  ·  "
                       f"{queue.get('active', 0)} active  ·  tests {quality.get('pytest', '?')}")
        else:
            summary = ("○  MIKACHAN   NOT INSTALLED\n"
                       "Run the one-line installer on a Debian/Ubuntu systemd server.\n"
                       f"CPU           {host.get('cpu_percent', '—')}%  ·  "
                       f"load {host.get('load_1m', '—')}\n"
                       f"Memory        {size(host.get('memory_used'))} / "
                       f"{size(host.get('memory_total'))}\n"
                       f"Free disk     {size(host.get('disk_free'))}")
        self.query_one("#status", Static).update(summary)


if __name__ == "__main__":
    MikachanPanel().run()
