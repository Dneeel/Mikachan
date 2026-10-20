"""Minimal, keyboard-only Mikachan terminal control panel.

Only uses Python's standard-library curses module. The independent deployment
manager remains operational from CLI if the screen or bot runtime is broken.
"""

from __future__ import annotations

import collections
import curses
import json
import os
import queue
import re
import textwrap
import threading
from dataclasses import dataclass
from typing import Any, Callable

from deploy.manager import BRANCH, EDITABLE, PRIVATE_KEYS, Manager, ManagerError

BANNER = '    ...     ..      ..        .          ..                                                                                               \n  x*8888x.:*8888: -"888:     @88>  < .z@8"`                              .uef^"                                              .x~~"*Weu.   \n X   48888X `8888H  8888     %8P    !@88E                              :d88E                      u.    u.                  d8Nu.  9888c  \nX8x.  8888X  8888X  !888>     .     \'888E   u          u           .   `888E             u      x@88k u@88c.                88888  98888  \nX8888 X8888  88888   "*8%-  .@88u    888E u@8NL     us888u.   .udR88N   888E .z8k     us888u.  ^"8888""8888"          .o@8  "***"  9888%  \n\'*888!X8888> X8888  xH8>   \'\'888E`   888E`"88*"  .@88 "8888" <888\'888k  888E~?888L .@88 "8888"   8888  888R       .o@8888%       ..@8*"   \n  `?8 `8888  X888X X888>     888E    888E .dN.   9888  9888  9888 \'Y"   888E  888E 9888  9888    8888  888R   .o@8888%"       ````"8Weu   \n  -^  \'888"  X888  8888>     888E    888E~8888   9888  9888  9888       888E  888E 9888  9888    8888  888R  8888R"          ..    ?8888L \n   dx \'88~x. !88~  8888>     888E    888E \'888&  9888  9888  9888       888E  888E 9888  9888    8888  888R  "*8888bu.     :@88N   \'8888N \n .8888Xf.888x:!    X888X.:   888&    888E  9888. 9888  9888  ?8888u../  888E  888E 9888  9888   "*88*" 8888"    ^"*8888bu. *8888~  \'8888F \n:""888":~"888"     `888*"    R888" \'"888*" 4888" "888*""888"  "8888P\'  m888N= 888> "888*""888"    ""   \'Y"          ^%*888 \'*8"`   9888%  \n    "~\'    "~        ""       ""      ""    ""    ^Y"   ^Y\'     "P\'     `Y"   888   ^Y"   ^Y\'                           ^%   `~===*%"`    \n                                                                             J88"                                                         \n                                                                             @%                                                           \n                                                                           :"                                                             '
COMPACT_BANNER = (
    ' __  __ ___ _  __    _    ____ _   _    _    _   _',
    '|  \\/  |_ _| |/ /   / \\  / ___| | | |  / \\  | \\ | |',
    "| |\\/| || || ' /   / _ \\| |   | |_| | / _ \\ |  \\| |",
    '| |  | || || . \\  / ___ \\ |___|  _  |/ ___ \\| |\\  |',
    '|_|  |_|___|_|\\_\\/_/   \\_\\____|_| |_/_/   \\_\\_| \\_|',
)
# Used for extremely narrow or shallow SSH terminals; never crop mid-character.
TINY_BANNER = ("[ M I K A C H A N ]",)

SETTING_GROUPS: dict[str, tuple[str, ...]] = {
    "Identity & Access": ("BOT_TOKEN", "OWNER_TELEGRAM_ID", "ALLOWED_TELEGRAM_IDS",
                          "ALLOWED_GROUP_IDS", "PRIVATE_CHATS_ONLY", "MOMMY_TELEGRAM_IDS"),
    "Telegram Userbot": ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "USERBOT_SESSION",
                         "COINPJ_SESSION", "USERBOT_STAGING_CHAT_ID"),
    "Translation & Data": ("TRANSLATION_API_URL", "TRANSLATION_API_KEY", "NASA_API_KEY",
                           "NAVASAN_API_KEY", "NAVASAN_REFRESH_SECONDS"),
    "Music": ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"),
    "Download & Limits": ("MAX_ACTIVE_DOWNLOADS", "MAX_ACTIVE_PER_USER", "MAX_LINKS_PER_MESSAGE",
                          "SEARCH_PAGE_SIZE", "SEARCH_SESSION_TTL_SECONDS", "CONFIRMATION_TTL_SECONDS",
                          "MIKACHAN_INSTAGRAM_COOKIES_FILE", "MIKACHAN_PORNHUB_COOKIES_FILE"),
    "Bot API": ("BOT_API_MODE", "LOCAL_BOT_API_URL"),
}

SENSITIVE_WORDS = ("TOKEN", "SECRET", "HASH", "SESSION", "KEY", "COOKIE", "PASSWORD")


def is_secret(key: str) -> bool:
    return key in PRIVATE_KEYS or any(word in key.upper() for word in SENSITIVE_WORDS)


def safe_setting_value(key: str, value: str) -> str:
    if is_secret(key):
        return "configured" if value and value != "(set)" else "not configured" if not value else "configured"
    return value if value else "not set"


def banner_lines(width: int, height: int) -> tuple[str, ...]:
    """Prefer the exact supplied art; preserve navigation in small terminals."""
    original = tuple(BANNER.splitlines())
    if width >= max(map(len, original)) + 2 and height >= len(original) + 11:
        return original
    if width >= max(map(len, COMPACT_BANNER)) + 2 and height >= 22:
        return COMPACT_BANNER
    return TINY_BANNER


def clip(value: Any, width: int) -> str:
    if width <= 0:
        return ""
    s = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    s = s.replace("\n", " ").replace("\r", " ").replace("\x1b", "")
    return s if len(s) <= width else s[:max(0, width - 1)] + "…"


def format_result(result: Any) -> list[str]:
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False, indent=2, default=str).splitlines()
    return str(result).splitlines() or ["(no result)"]


@dataclass(frozen=True)
class Entry:
    label: str
    action: str
    payload: Any = None


@dataclass
class Page:
    title: str
    entries: list[Entry]
    selected: int = 0
    offset: int = 0
    kind: str = "menu"
    text: list[str] | None = None


class MikachanMenu:
    """A self-contained up/down/enter navigator around the existing Manager."""

    def __init__(self, manager_factory: Callable[..., Manager] = Manager):
        self.manager_factory = manager_factory
        self.pages = [self._root_page()]
        self.notifications: collections.deque[str] = collections.deque(maxlen=8)
        self.events: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self.running = True
        self.worker: threading.Thread | None = None
        self.status: dict[str, Any] = {}
        self.form_label: str | None = None
        self.form_value = ""
        self.form_secret = False
        self.form_callback: Callable[[str], None] | None = None
        self.form_guidance = ""
        self.state: dict[str, Any] = {}
        self._pending_result: str | None = None

    @property
    def page(self) -> Page:
        return self.pages[-1]

    def _root_page(self) -> Page:
        return Page("MAIN MENU", [
            Entry("Bot & Health", "go:bot"),
            Entry("Releases & Updates", "go:releases"),
            Entry("Backups & Recovery", "go:backups"),
            Entry("Settings & Access", "go:settings"),
            Entry("Host & Maintenance", "go:host"),
            Entry("Exit", "quit"),
        ])

    @staticmethod
    def submenu(which: str) -> Page:
        menus: dict[str, tuple[str, list[tuple[str, str]]]] = {
            "bot": ("BOT & HEALTH", [
                ("Service status", "op:status"), ("Start bot", "op:start"),
                ("Stop bot", "confirm:stop"), ("Restart bot", "op:restart"),
                ("Queue activity", "op:queue"), ("Recent logs", "op:logs"),
                ("Live diagnostics", "op:diagnostics"),
            ]),
            "releases": ("RELEASES & UPDATES", [
                ("Available branches / Select version", "op:versions"),
                ("Update active branch", "confirm:update"),
                ("Switch to a specific commit", "input:commit_branch"),
                ("Rollback code + data", "confirm:rollback"),
                ("Preview release cleanup", "op:prune-preview"),
                ("Prune old releases", "confirm:prune"),
            ]),
            "backups": ("BACKUPS & RECOVERY", [
                ("Create a backup", "op:backup"),
                ("Browse / restore backups", "op:backups"),
                ("Storage overview", "op:storage"),
                ("Recover interrupted update", "confirm:recover"),
                ("Clear interrupted media", "confirm:cleanup"),
            ]),
            "settings": ("SETTINGS & ACCESS", [
                ("Guided initial setup", "setup"),
                ("Edit settings", "go:categories"),
                ("Review pending co-owner requests", "op:owner-requests"),
                ("Configuration summary", "op:config"),
            ]),
            "host": ("HOST & MAINTENANCE", [
                ("Host preflight", "op:preflight"),
                ("Install missing host tools", "confirm:deps"),
                ("Install bot release (choose version)", "op:versions"),
                ("Remove bot code (keep data)", "confirm:remove"),
                ("Permanently delete managed project", "confirm:purge"),
            ]),
        }
        title, operations = menus[which]
        return Page(title, [Entry(*item) for item in operations] + [Entry("Back", "back")])

    def push(self, page: Page) -> None:
        self.pages.append(page)

    def back(self) -> None:
        if self.form_label is not None:
            self.close_form()
        elif len(self.pages) > 1:
            self.pages.pop()
            if self.page.title in (name.upper() for name in SETTING_GROUPS):
                name = next(name for name in SETTING_GROUPS if name.upper() == self.page.title)
                try: self.state["config"] = self.manager_factory().config()
                except (ManagerError, OSError, ValueError): pass
                config = self.state.get("config", {})
                self.page.entries = [Entry(f"{key}  :  {safe_setting_value(key, config.get(key, ''))}",
                                           "setting", key) for key in SETTING_GROUPS[name]
                                     if key in EDITABLE] + [Entry("Back", "back")]
        else:
            self.running = False

    def notify(self, value: str) -> None:
        self.notifications.append(value)

    def _status_line(self) -> str:
        if not self.status:
            return "SERVER: status not checked"
        if not self.status.get("installed"):
            return "SERVER: not installed"
        state = self.status.get("service", "unknown")
        branch = self.status.get("branch", "?")
        sha = self.status.get("commit", "")[:12]
        return f"SERVER: {state.upper()}  |  {branch}@{sha}  |  {'OK' if self.status.get('consistent') else 'INSPECT'}"

    def open_form(self, label: str, callback: Callable[[str], None], *, secret: bool = False,
                  guidance: str = "", default: str = "") -> None:
        self.form_label = label
        self.form_callback = callback
        self.form_secret = secret
        self.form_guidance = guidance
        self.form_value = default

    def close_form(self) -> None:
        self.form_label = None
        self.form_value = ""
        self.form_callback = None
        self.form_secret = False
        self.form_guidance = ""

    def submit_form(self) -> None:
        callback, answer = self.form_callback, self.form_value.strip()
        self.close_form()
        if callback:
            callback(answer)

    def _confirm(self, action: str, payload: Any = None) -> None:
        words = {
            "stop": "STOP", "update": "UPDATE", "switch": "SWITCH", "rollback": "ROLLBACK",
            "prune": "PRUNE", "restore": "RESTORE", "recover": "RECOVER", "cleanup": "CLEAN",
            "deps": "INSTALL TOOLS", "install": "INSTALL", "remove": "REMOVE MIKACHAN",
            "purge": "PURGE MIKACHAN", "owner-approve": "APPROVE OWNER", "setup": "SETUP",
        }
        warnings = {
            "stop": "Stops all bot responses until restarted.",
            "update": "Installs latest checked commit on the ACTIVE branch. Saves snapshot first.",
            "switch": f"Activates {payload}; keeps a snapshot for recovery.",
            "rollback": "DANGER: Restores old code AND OLD DATABASE. Newer changes WILL BE LOST.",
            "restore": f"DANGER: Overwrites present data with snapshot {payload}; saves undo first.",
            "purge": "DANGER: Permanently deletes code, config, database AND LOCAL backups.",
            "owner-approve": "Privileged role change; the request must originate from primary owner.",
            "cleanup": "Requires stopped service; discards abandoned media jobs.",
            "install": "Only available when no managed bot is already installed.",
        }
        phrase = words[action]
        self.open_form("Type " + phrase + " to continue", lambda value: (
            self.execute(action, payload) if value == phrase else self.notify("Cancelled: confirmation mismatch")),
            guidance=warnings.get(action, "Confirm this system operation."))

    def _choose_setting(self, key: str) -> None:
        if key not in EDITABLE:
            self.notify("Read-only setting")
            return
        def edit(value: str) -> None:
            self.execute("set", {"key": key, "value": value})
        self.open_form(f"New value for {key}", edit, secret=is_secret(key),
                       guidance="Empty = clear if allowed. Existing secret is never revealed.",
                       default="" if is_secret(key) else self.state.get("config", {}).get(key, ""))

    def _choose_snapshot(self, snapshot: str) -> None:
        self._confirm("restore", snapshot)

    def _choose_branch(self, item: dict[str, str]) -> None:
        branch, commit = item["branch"], item["commit"]
        installed = self.status.get("installed")
        if installed is None:
            try: installed = self.manager_factory().status().get("installed", False)
            except (ManagerError, OSError, ValueError): installed = False
        if not installed:
            entries = [Entry(f"Install {branch}@{commit[:12]}", "confirm:install", branch),
                       Entry("Back", "back")]
        else:
            entries = [Entry(f"Activate latest {branch}@{commit[:12]}", "confirm:switch", (branch, None)),
                       Entry("Choose different commit SHA", "input:commit", branch),
                       Entry("Back", "back")]
        self.push(Page(f"RELEASE {branch}", entries))

    def _choose_owner_request(self, item: dict[str, Any]) -> None:
        self._confirm("owner-approve", (item["token"], int(item["target_id"])))

    def _open_categories(self) -> None:
        try:
            self.state["config"] = self.manager_factory().config()
        except (ManagerError, OSError, ValueError) as exc:
            self.notify(f"Cannot load configuration: {type(exc).__name__}")
            return
        self.push(Page("SETTING CATEGORIES", [Entry(name, "category", name)
                                                for name in SETTING_GROUPS] + [Entry("Back", "back")]))

    def _open_category(self, name: str) -> None:
        try:
            self.state["config"] = self.manager_factory().config()
        except (ManagerError, OSError, ValueError) as exc:
            self.notify(f"Cannot load settings: {type(exc).__name__}")
            return
        config = self.state.get("config", {})
        self.push(Page(name.upper(), [Entry(f"{key}  :  {safe_setting_value(key, config.get(key, ''))}",
                                          "setting", key)
                                     for key in SETTING_GROUPS[name] if key in EDITABLE]
                       + [Entry("Back", "back")]))

    def select(self) -> None:
        if self.worker and self.worker.is_alive():
            self.notify("Operation still in progress; changes are locked")
            return
        page = self.page
        if page.kind == "report":
            self.back()
            return
        if not page.entries:
            self.back()
            return
        entry = page.entries[page.selected]
        cmd = entry.action
        if cmd == "back": self.back()
        elif cmd == "quit": self.running = False
        elif cmd.startswith("go:"):
            target = cmd.partition(":")[2]
            if target == "categories": self._open_categories()
            else: self.push(self.submenu(target))
        elif cmd == "category": self._open_category(entry.payload)
        elif cmd == "setting": self._choose_setting(entry.payload)
        elif cmd == "snapshot": self._choose_snapshot(entry.payload)
        elif cmd == "branch": self._choose_branch(entry.payload)
        elif cmd == "owner-request": self._choose_owner_request(entry.payload)
        elif cmd == "setup": self.start_setup()
        elif cmd.startswith("op:"): self.execute(cmd.partition(":")[2])
        elif cmd.startswith("confirm:"): self._confirm(cmd.partition(":")[2], entry.payload)
        elif cmd == "input:commit_branch":
            self.open_form("Existing branch (e.g. v2 or v2-test)", self._commit_branch)
        elif cmd == "input:commit":
            self._commit_input(entry.payload)

    def _commit_branch(self, branch: str) -> None:
        if not BRANCH.fullmatch(branch):
            self.notify("Expected a branch such as v2 or v2-test")
            return
        self._commit_input(branch)

    def _commit_input(self, branch: str) -> None:
        self.open_form(f"Full 40-character commit SHA on {branch}",
                       lambda sha: self._confirm("switch", (branch, sha)) if re.fullmatch(
                           r"[0-9a-f]{40}", sha) else self.notify("Invalid full commit SHA"))

    def start_setup(self) -> None:
        self.state["setup"] = {}
        self.state["setup_step"] = 0
        try:
            if not self.manager_factory().status().get("installed"):
                self.notify("Install the managed release first")
                return
            already = self.manager_factory().status().get("config", False)
        except (ManagerError, OSError) as exc:
            self.notify(f"Cannot start setup: {type(exc).__name__}")
            return
        if already:
            self.open_form("Type SETUP to reconfigure",
                           lambda value: self._setup_next() if value == "SETUP" else
                           self.notify("Cancelled: confirmation mismatch"),
                           guidance="Existing bot credentials and owner may change; a snapshot is created.")
        else:
            self._setup_next()

    def _setup_next(self) -> None:
        steps = [
            ("token", "BotFather bot token", True),
            ("owner", "Primary owner's numeric Telegram ID", False),
            ("users", "Other approved user IDs (comma-separated, optional)", False),
            ("groups", "Allowed group IDs (comma-separated, optional)", False),
            ("api_id", "Telegram API ID (optional; userbot)", False),
            ("api_hash", "Telegram API hash (optional; userbot)", True),
        ]
        step = self.state["setup_step"]
        if step >= len(steps):
            data = self.state.pop("setup")
            self.state.pop("setup_step", None)
            token, owner = data.get("token", ""), data.get("owner", "")
            if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
                self.notify("Invalid bot token; restart Setup")
                return
            if not owner.isdigit() or int(owner) <= 0:
                self.notify("Invalid owner user ID; restart Setup")
                return
            users = [s.strip() for s in data.get("users", "").split(",") if s.strip()]
            groups = [s.strip() for s in data.get("groups", "").split(",") if s.strip()]
            if any(not s.isdigit() or int(s) <= 0 for s in users) or len(set(users + [owner])) > 4:
                self.notify("Invalid users (max 4 distinct numeric IDs); restart Setup")
                return
            if any(not re.fullmatch(r"-[0-9]+", s) for s in groups):
                self.notify("Invalid negative group IDs; restart Setup")
                return
            if bool(data.get("api_id")) != bool(data.get("api_hash")) or (
                data.get("api_id") and not data["api_id"].isdigit()
            ):
                self.notify("API ID and hash must be supplied together; restart Setup")
                return
            config = {"BOT_TOKEN": token, "OWNER_TELEGRAM_ID": owner,
                      "ALLOWED_TELEGRAM_IDS": ",".join(dict.fromkeys([owner] + users)),
                      "ALLOWED_GROUP_IDS": ",".join(groups)}
            if groups: config["PRIVATE_CHATS_ONLY"] = "false"
            if data.get("api_id"):
                config.update(TELEGRAM_API_ID=data["api_id"], TELEGRAM_API_HASH=data["api_hash"])
            self.execute("setup", config)
            return
        key, label, secret = steps[step]
        self.open_form(f"SETUP {step + 1}/6  -  {label}", lambda value: self._setup_answer(key, value),
                       secret=secret, guidance="Enter to continue / Esc to cancel setup")

    def _setup_answer(self, key: str, value: str) -> None:
        if key in ("token", "owner") and not value:
            self.notify("Required field; setup cancelled")
            self.state.pop("setup", None)
            return
        self.state["setup"][key] = value
        self.state["setup_step"] += 1
        self._setup_next()

    def _work(self, action: str, payload: Any) -> None:
        def progress(message: str) -> None:
            # Never log entered credentials or raw configuration values.
            self.events.put(("progress", message))
        try:
            manager = self.manager_factory(on_event=progress)
            if action == "status": result = manager.status()
            elif action == "start": result = manager.service("start")
            elif action == "stop": result = manager.service("stop")
            elif action == "restart": result = manager.service("restart")
            elif action == "queue": result = manager.queue()
            elif action == "logs": result = manager.logs()
            elif action == "diagnostics": result = manager.diagnostics(live=True)
            elif action == "versions": result = manager.versions(refresh=True)
            elif action == "update": result = manager.update()
            elif action == "switch": result = manager.switch(payload[0], commit=payload[1])
            elif action == "rollback": result = manager.rollback()
            elif action == "prune-preview": result = manager.prune_releases()
            elif action == "prune": result = manager.prune_releases(apply=True)
            elif action == "backup": result = manager.backup()
            elif action == "backups": result = manager.backups()
            elif action == "storage": result = manager.storage()
            elif action == "recover": result = manager.recover()
            elif action == "cleanup": result = manager.cleanup_transient()
            elif action == "owner-requests": result = manager.owner_approvals()
            elif action == "owner-approve": result = manager.approve_owner(*payload)
            elif action == "config": result = manager.config()
            elif action == "set": result = manager.set_config(payload["key"], payload["value"])
            elif action == "preflight": result = manager.preflight()
            elif action == "deps": result = manager.install_os_dependencies()
            elif action == "install":
                if not isinstance(payload, str) or not BRANCH.fullmatch(payload):
                    raise ManagerError("Choose a version before installing")
                result = manager.install(branch=payload, reuse_existing_config=True)
            elif action == "setup": result = manager.setup(payload)
            elif action == "remove": result = manager.remove()
            elif action == "purge": result = manager.remove(purge=True)
            else: raise ManagerError("Unsupported action")
            # status refresh is always separate from action result
            if action == "status": status = result
            else:
                try: status = manager.status()
                except (ManagerError, OSError, ValueError): status = {}
            self.events.put(("done", (action, result, status)))
        except (ManagerError, OSError, ValueError, PermissionError) as exc:
            self.events.put(("failure", f"{type(exc).__name__}: {exc}"))
        except Exception as exc:
            # Deliberately don't expose exception details/stack traces containing secrets.
            self.events.put(("failure", f"Unexpected {type(exc).__name__}; inspect local server logs"))

    def execute(self, action: str, payload: Any = None) -> None:
        if self.worker and self.worker.is_alive():
            self.notify("An operation is already in progress")
            return
        self.notifications.clear()
        self.notify(f"Running {action} ...")
        self._pending_result = "silent" if action == "status" and payload == "silent" else None
        self.worker = threading.Thread(target=self._work, args=(action, payload),
                                       name="mikachan-manager-operation", daemon=False)
        self.worker.start()

    def drain_events(self) -> None:
        while True:
            try: kind, data = self.events.get_nowait()
            except queue.Empty: return
            if kind == "progress": self.notify(clip(data, 140))
            elif kind == "failure":
                self.worker = None
                self.push(Page("OPERATION FAILED", [Entry("Back", "back")], kind="report",
                               text=str(data).splitlines()))
            elif kind == "done":
                self.worker = None
                action, result, status = data
                self.status = status
                if action == "status" and self._pending_result == "silent":
                    self._pending_result = None
                    self.notify("Status refreshed")
                    continue
                if action == "config" and isinstance(result, dict):
                    self.state["config"] = result
                if action == "versions" and isinstance(result, list):
                    entries = [Entry(f"{item['branch']}   {item['commit'][:12]}", "branch", item)
                               for item in result if isinstance(item, dict)
                               and "branch" in item and "commit" in item]
                    self.push(Page("AVAILABLE VERSIONS", entries + [Entry("Back", "back")]))
                elif action == "backups" and isinstance(result, list):
                    entries = [Entry(snapshot, "snapshot", snapshot) for snapshot in result]
                    self.push(Page("BACKUP SNAPSHOTS", entries + [Entry("Back", "back")]))
                elif action == "owner-requests" and isinstance(result, list):
                    entries = [Entry(f"{item['action']}  {item['target_id']}   by {item['requester_id']}",
                                     "owner-request", item)
                               for item in result if isinstance(item, dict)]
                    self.push(Page("PENDING OWNER REQUESTS", entries + [Entry("Back", "back")]))
                else:
                    self.push(Page(f"RESULT: {action.upper()}", [Entry("Back", "back")],
                                   kind="report", text=format_result(result)))

    def _safe_add(self, win: Any, row: int, col: int, value: str, style: int = 0) -> None:
        height, width = win.getmaxyx()
        if 0 <= row < height and col < width and col >= 0:
            try:
                win.addnstr(row, col, clip(value, max(0, width - col - 1)),
                            max(0, width - col - 1), style)
            except curses.error:
                pass

    def draw(self, win: Any) -> None:
        win.erase()
        height, width = win.getmaxyx()
        if height < 10 or width < 30:
            self._safe_add(win, 0, 0, "Enlarge terminal (min 30x10)")
            win.refresh()
            return
        header = banner_lines(width, height)
        y = 1
        for line in header:
            self._safe_add(win, y, max(0, (width - len(line.rstrip())) // 2), line.rstrip(),
                           curses.A_BOLD)
            y += 1
        y += 1
        page = self.page
        self._safe_add(win, y, 2, page.title, curses.A_BOLD)
        y += 2
        # reserve two footer rows for navigation / progress
        available = max(1, height - y - 4)
        if page.kind == "report":
            lines = []
            for raw in (page.text or []):
                lines.extend(textwrap.wrap(raw, width=max(8, width - 8),
                                           replace_whitespace=False, drop_whitespace=False,
                                           break_long_words=True, break_on_hyphens=False) or [""])
            page.offset = min(page.offset, max(0, len(lines) - available))
            for line in lines[page.offset:page.offset + available]:
                self._safe_add(win, y, 4, line)
                y += 1
        else:
            page.selected = min(page.selected, len(page.entries) - 1) if page.entries else 0
            if page.selected < page.offset: page.offset = page.selected
            if page.selected >= page.offset + available: page.offset = page.selected - available + 1
            for index in range(page.offset, min(page.offset + available, len(page.entries))):
                entry = page.entries[index]
                selected = index == page.selected
                style = curses.A_REVERSE | curses.A_BOLD if selected else curses.A_NORMAL
                self._safe_add(win, y, 4, ("> " if selected else "  ") + entry.label, style)
                y += 1
        if self.form_label is not None:
            line = height - 4
            self._safe_add(win, line - 1, 2, self.form_guidance, curses.A_DIM)
            visible = "*" * len(self.form_value) if self.form_secret else self.form_value
            label = clip(self.form_label + ": ", max(15, width // 2))
            tail = visible[-max(1, width - len(label) - 5):]
            self._safe_add(win, line, 2, label + tail, curses.A_REVERSE)
            footer = "Enter Confirm   Esc Cancel   Backspace Edit"
        else:
            footer = "↑ ↓ Move    Enter Select    Esc Back    Q Exit"
        self._safe_add(win, height - 2, 2, footer, curses.A_DIM)
        if self.worker and self.worker.is_alive():
            self._safe_add(win, height - 1, 2, "Working — do not close terminal during update/restore", curses.A_BOLD)
        elif self.notifications:
            self._safe_add(win, height - 1, 2, self.notifications[-1], curses.A_DIM)
        win.refresh()

    def handle_key(self, key: int) -> None:
        if self.form_label is not None:
            if key in (27,):
                self.close_form()
                self.state.pop("setup", None)
                self.state.pop("setup_step", None)
            elif key in (10, 13, curses.KEY_ENTER): self.submit_form()
            elif key in (curses.KEY_BACKSPACE, 127, 8): self.form_value = self.form_value[:-1]
            elif key == 21: self.form_value = ""  # Ctrl+U
            elif 32 <= key <= 126 and len(self.form_value) < 4096:
                self.form_value += chr(key)
            return
        if self.worker and self.worker.is_alive():
            return
        page = self.page
        if key in (curses.KEY_UP, ord("k")):
            if page.kind == "report": page.offset = max(0, page.offset - 1)
            elif page.entries: page.selected = max(0, page.selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            if page.kind == "report": page.offset += 1
            elif page.entries: page.selected = min(len(page.entries) - 1, page.selected + 1)
        elif key in (10, 13, curses.KEY_ENTER): self.select()
        elif key in (27, curses.KEY_LEFT, 127):
            if len(self.pages) > 1: self.back()
        elif key in (ord("q"), ord("Q")):
            if len(self.pages) == 1: self.running = False
            else: self.back()
        elif key in (ord("r"), ord("R")) and self.form_label is None:
            self.execute("status", "silent")

    def run(self, win: Any) -> None:
        try: curses.curs_set(0)
        except curses.error: pass
        win.keypad(True)
        win.timeout(125)
        try:
            self.status = self.manager_factory().status()
        except (ManagerError, OSError, ValueError) as exc:
            self.notify(f"Status unavailable: {type(exc).__name__}")
        while self.running:
            self.drain_events()
            self.draw(win)
            try: key = win.getch()
            except KeyboardInterrupt:
                if not (self.worker and self.worker.is_alive()): self.back()
                continue
            if key != -1: self.handle_key(key)
        if self.worker and self.worker.is_alive():
            self.worker.join()


def main() -> None:
    if not os.isatty(0) or not os.isatty(1):
        raise SystemExit("Interactive terminal required; use `mikachan status` for CLI mode")
    if not os.environ.get("TERM") or os.environ.get("TERM") == "dumb":
        raise SystemExit("Terminal does not support cursor controls (TERM=dumb)")
    try:
        curses.wrapper(lambda win: MikachanMenu().run(win))
    except curses.error as exc:
        raise SystemExit(f"Unsupported terminal capability: {exc}") from exc


if __name__ == "__main__":
    main()
