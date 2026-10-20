"""Standalone, dependency-free deployment engine for Mikachan on systemd hosts.

The Telegram runtime is deliberately never imported here.  A manager can repair a
broken release even when that release's Python environment cannot start.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes.util
import fcntl
import getpass
import grp
import json
import os
import pwd
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator


OFFICIAL_REPO = "https://github.com/Dneeel/Mikachan.git"
BRANCH = re.compile(r"(?:v[1-9][0-9]*|v2-test)\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
SNAPSHOT = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\Z")
MANAGED = "Mikachan manager schema 1\n"
EDITABLE = {
    "BOT_TOKEN", "OWNER_TELEGRAM_ID", "ALLOWED_TELEGRAM_IDS", "ALLOWED_GROUP_IDS",
    "MOMMY_TELEGRAM_IDS", "BOT_API_MODE", "LOCAL_BOT_API_URL",
    "TELEGRAM_API_ID", "TELEGRAM_API_HASH",
    "PRIVATE_CHATS_ONLY", "MAX_ACTIVE_DOWNLOADS", "MAX_ACTIVE_PER_USER",
    "MAX_LINKS_PER_MESSAGE", "SEARCH_PAGE_SIZE", "SEARCH_SESSION_TTL_SECONDS",
    "CONFIRMATION_TTL_SECONDS", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
    "NASA_API_KEY", "NAVASAN_API_KEY", "NAVASAN_REFRESH_SECONDS",
    "TRANSLATION_API_URL", "TRANSLATION_API_KEY",
    "COINPJ_SESSION", "USERBOT_SESSION", "USERBOT_STAGING_CHAT_ID",
    "MIKACHAN_INSTAGRAM_COOKIES_FILE", "MIKACHAN_PORNHUB_COOKIES_FILE",
}
ENV_KEYS = EDITABLE | {
    "OWNER_TELEGRAM_ID", "BOT_API_MODE", "LOCAL_BOT_API_URL", "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH", "DATA_DIR", "FREESTYLE_API_KEY", "HTTP_PROXY",
    "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE",
}
PRIVATE_KEYS = {"BOT_TOKEN", "SPOTIFY_CLIENT_SECRET", "NASA_API_KEY", "NAVASAN_API_KEY",
                "TRANSLATION_API_KEY", "TELEGRAM_API_HASH", "COINPJ_SESSION", "USERBOT_SESSION",
                "MIKACHAN_INSTAGRAM_COOKIES_FILE", "MIKACHAN_PORNHUB_COOKIES_FILE",
                "FREESTYLE_API_KEY", "HTTP_PROXY", "HTTPS_PROXY"}


class ManagerError(RuntimeError):
    """A safe, actionable operator-facing failure."""


@dataclass(frozen=True)
class Paths:
    base: Path = Path("/opt/Mikachan")
    state: Path = Path("/opt/Mikachan/state")
    backups: Path = Path("/opt/Mikachan/backups")
    env: Path = Path("/opt/Mikachan/config/bot.env")
    unit: Path = Path("/etc/systemd/system/mikachan.service")

    @property
    def repo(self) -> Path:
        return self.base / "repository.git"

    @property
    def releases(self) -> Path:
        return self.base / "releases"

    @property
    def current(self) -> Path:
        return self.base / "current"

    @property
    def manifest(self) -> Path:
        return self.base / "manifest.json"

    @property
    def marker(self) -> Path:
        return self.base / ".managed-by-mikachan"

    @property
    def journal(self) -> Path:
        return self.base / "transaction.json"

    @property
    def temporary(self) -> Path:
        return self.base / "tmp"

    @property
    def runtime_temp(self) -> Path:
        return self.temporary / "runtime"

    @property
    def manager_temp(self) -> Path:
        return self.temporary / "manager"

    @property
    def cache(self) -> Path:
        return self.base / "cache"

    @property
    def logs(self) -> Path:
        return self.base / "logs"


def _atomic(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".mikachan-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
        if not match:
            raise ManagerError(f"Invalid environment line {number} in {path}")
        key, value = match.groups()
        if key in values:
            raise ManagerError(f"Duplicate environment key: {key}")
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError) as exc:
                raise ManagerError(f"Invalid quoted value for {key}") from exc
            if not isinstance(value, str):
                raise ManagerError(f"Invalid value for {key}")
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ManagerError(f"Control character in {key}")
        values[key] = value
    return values


def _render_env(values: dict[str, str]) -> str:
    lines = []
    for key, value in sorted(values.items()):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ManagerError("Invalid environment key")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ManagerError(f"Control character in {key}")
        lines.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def _ids(raw: str, *, negative: bool = False) -> set[int]:
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if any(not re.fullmatch(r"-?[0-9]+", part) for part in parts):
        raise ManagerError("IDs must be comma-separated numbers")
    result = {int(part) for part in parts}
    if len(result) != len(parts) or any((value >= 0 if negative else value <= 0)
                                          for value in result):
        raise ManagerError("IDs must be distinct and have the expected sign")
    return result


class Manager:
    def __init__(self, paths: Paths = Paths(), *, repo_url: str | None = None,
                 require_root: bool = True, uv: str | None = None,
                 on_event: Callable[[str], None] | None = None):
        self.paths = paths
        if repo_url is None:
            options = paths.base / "config/manager.json"
            try:
                repo_url = json.loads(options.read_text(encoding="utf-8")).get(
                    "repo_url", OFFICIAL_REPO) if options.is_file() else OFFICIAL_REPO
            except (OSError, ValueError, AttributeError) as exc:
                raise ManagerError("Manager repository configuration is unreadable") from exc
        if not isinstance(repo_url, str) or not repo_url:
            raise ManagerError("Manager repository URL is invalid")
        self.repo_url = repo_url
        self.require_root = require_root
        panel_uv = paths.base / "panel-venv/bin/uv"
        self.uv = (uv or
                   (str(panel_uv) if panel_uv.is_file() and os.access(panel_uv, os.X_OK)
                    else None) or shutil.which("uv") or "uv")
        self.on_event = on_event or (lambda message: None)

    def _root(self) -> None:
        if self.require_root and os.geteuid() != 0:
            raise ManagerError("Run this operation with sudo; status and preflight are read-only")

    def _run(self, *args: str, cwd: Path | None = None, timeout: int = 60,
             check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                                    text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ManagerError(f"Could not run {args[0]}: {type(exc).__name__}") from exc
        if check and result.returncode:
            detail = (result.stderr or result.stdout).strip()[-1000:]
            raise ManagerError(f"{args[0]} failed ({result.returncode}): {detail}")
        return result

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        self._root()
        if self.paths.base.is_symlink():
            raise ManagerError("Managed installation directory cannot be a symlink")
        self.paths.base.mkdir(parents=True, exist_ok=True)
        with (self.paths.base / ".manager.lock").open("a+") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ManagerError("Another Mikachan management operation is running") from exc
            yield

    def _managed(self) -> None:
        if self.paths.base.is_symlink() or not self.paths.marker.is_file() or \
                self.paths.marker.read_text(encoding="utf-8") != MANAGED:
            raise ManagerError("This is not a panel-managed installation; no files were changed")

    def _manifest(self) -> dict[str, Any]:
        self._managed()
        try:
            data = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
            if data["schema"] != 1 or not data["history"]:
                raise ValueError("invalid manifest")
            return data
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise ManagerError("Installation manifest is missing or invalid") from exc

    def _save_manifest(self, data: dict[str, Any]) -> None:
        _atomic(self.paths.manifest, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def _ensure_consistent(self, manifest: dict[str, Any]) -> None:
        if self.paths.journal.exists():
            raise ManagerError("Interrupted activation found; run recover before changing releases")
        if not self.paths.current.is_symlink() or \
                self.paths.current.resolve() != self.paths.releases / manifest["history"][-1]["sha"]:
            raise ManagerError("Current release does not match manifest; inspect before changing releases")

    def _systemctl(self, action: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self._run("systemctl", action, "mikachan.service", check=check, timeout=90)

    def _active(self) -> bool:
        try:
            return self._systemctl("is-active", check=False).stdout.strip() == "active"
        except ManagerError:
            return False

    def preflight(self) -> dict[str, Any]:
        tools = {name: shutil.which(name) for name in
                 ("git", "systemctl", "flock", "ffmpeg", "ffprobe", "tesseract")}
        # Read-only commands launch with the system Python, outside the panel
        # venv. Report the same uv executable that installation will use.
        tools["uv"] = shutil.which(self.uv)
        disk = shutil.disk_usage(self.paths.base.parent) if self.paths.base.parent.exists() else None
        languages = self._run("tesseract", "--list-langs", check=False).stdout.splitlines() \
            if tools["tesseract"] else []
        return {"platform": sys.platform, "root": os.geteuid() == 0,
                "tools": tools, "free_bytes": disk.free if disk else None,
                "systemd": Path("/run/systemd/system").exists(),
                "persian_ocr": "fas" in languages,
                "cairo": ctypes.util.find_library("cairo") is not None}

    def _require_host(self) -> None:
        check = self.preflight()
        missing = [name for name, path in check["tools"].items() if not path]
        if not check["systemd"]:
            raise ManagerError("systemd is not PID 1; this installer needs a systemd host")
        if missing:
            raise ManagerError("Missing host tools: " + ", ".join(missing) +
                               ". Install OS dependencies first")
        if not check["persian_ocr"] or not check["cairo"]:
            raise ManagerError("Persian OCR language data or Cairo is missing; install OS dependencies")
        if check["free_bytes"] is not None and check["free_bytes"] < 2_000_000_000:
            raise ManagerError("At least 2 GB free disk is required to stage a release")

    def install_os_dependencies(self) -> str:
        """Install only fixed, reviewed Debian/Ubuntu packages on operator request."""
        self._root()
        with Path("/etc/os-release").open(encoding="utf-8") as file:
            system = file.read()
        if not re.search(r'^ID="?(debian|ubuntu)"?$', system, re.MULTILINE):
            raise ManagerError("Automatic OS dependency installation supports Debian/Ubuntu only")
        self.on_event("Refreshing Debian/Ubuntu package index…")
        self._run("apt-get", "update", "-y", timeout=600)
        self.on_event("Installing Git, media tools, OCR and Cairo runtime…")
        self._run("apt-get", "install", "-y", "git", "ffmpeg", "tesseract-ocr",
                  "tesseract-ocr-fas", "libcairo2", "python3-venv", timeout=900)
        return "Installed required Debian/Ubuntu OS packages; rerun preflight"

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {"installed": False, "service": "active" if self._active()
                                  else "inactive", "managed": self.paths.marker.is_file()}
        try:
            def cpu_ticks() -> tuple[int, int]:
                ticks = [int(value) for value in Path("/proc/stat").read_text(
                    encoding="ascii").splitlines()[0].split()[1:]]
                return sum(ticks), ticks[3] + ticks[4]
            total_a, idle_a = cpu_ticks()
            time.sleep(0.12)
            total_b, idle_b = cpu_ticks()
            cpu = round(100 * (1 - (idle_b - idle_a) / (total_b - total_a)), 1) \
                if total_b > total_a else None
            memory = Path("/proc/meminfo").read_text(encoding="ascii")
            fields = {key: int(value.split()[0]) * 1024 for key, value in
                      (line.split(":", 1) for line in memory.splitlines() if ":" in line)}
            result["host"] = {"cpu_percent": cpu,
                              "load_1m": round(os.getloadavg()[0], 2),
                              "memory_used": fields["MemTotal"] - fields["MemAvailable"],
                              "memory_total": fields["MemTotal"],
                              "disk_free": shutil.disk_usage(self.paths.base.parent).free}
        except (OSError, KeyError, ValueError):
            result["host"] = {}
        if not self.paths.manifest.is_file():
            result["incomplete_install"] = self.paths.marker.is_file()
            result["legacy_detected"] = not result["incomplete_install"] and (
                self.paths.unit.exists() or (
                    self.paths.base.is_dir() and not self.paths.marker.is_file() and
                    any(item.name != ".manager.lock" for item in self.paths.base.iterdir())
                )
            )
            return result
        manifest = self._manifest()
        current = manifest["history"][-1]
        result.update(installed=True, branch=current["branch"], commit=current["sha"],
                      installed_at=current["at"], releases=len(manifest["history"]),
                      database=self.paths.state.joinpath("mikachan.sqlite").exists(),
                      interrupted=self.paths.journal.exists(),
                      config=self.paths.env.exists(),
                      setup_required=not self.paths.env.exists())
        result["consistent"] = self.paths.current.is_symlink() and \
            self.paths.current.resolve() == self.paths.releases / current["sha"]
        quality = self.paths.releases / current["sha"] / ".manager-quality.json"
        if quality.is_file():
            try:
                result["quality"] = json.loads(quality.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result["quality"] = {"status": "unreadable"}
        database = self.paths.state / "mikachan.sqlite"
        if database.is_file():
            try:
                with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
                    result["authorized_users"] = db.execute("SELECT COUNT(*) FROM access").fetchone()[0]
                    result["authorized_groups"] = db.execute(
                        "SELECT COUNT(*) FROM allowed_groups").fetchone()[0]
                    counts = {state: count for state, count in db.execute(
                        "SELECT state, COUNT(*) FROM jobs GROUP BY state")}
                    result["queue"] = {"waiting": sum(counts.get(state, 0) for state in
                                                   ("QUEUED", "RETRYABLE")),
                                       "active": sum(counts.get(state, 0) for state in
                                                  ("RESOLVING", "DOWNLOADING", "VERIFYING",
                                                   "DELIVERING"))}
            except sqlite3.Error:
                result["database_status"] = "unreadable"
        if self.paths.repo.exists():
            latest = self._run("git", "--git-dir", str(self.paths.repo), "rev-parse", "--verify",
                               f"refs/remotes/origin/{current['branch']}^{{commit}}", check=False)
            result["known_remote_commit"] = latest.stdout.strip() if latest.returncode == 0 else None
        return result

    def queue(self) -> dict[str, Any]:
        """Read queue metadata only; URLs and user identities stay out of the panel."""
        database = self.paths.state / "mikachan.sqlite"
        if not database.is_file():
            return {"message": "No database yet", "states": {}, "recent": []}
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2) as db:
                states = {state: count for state, count in db.execute(
                    "SELECT state, COUNT(*) FROM jobs GROUP BY state")}
                recent = [{"id": jid, "state": state, "updated": int(updated)}
                          for jid, state, updated in db.execute(
                              "SELECT id, state, updated FROM jobs ORDER BY updated DESC LIMIT 8")]
            return {"states": states, "recent": recent}
        except sqlite3.Error as exc:
            raise ManagerError("Queue database is unavailable") from exc

    def storage(self) -> dict[str, Any]:
        base = self.paths.base
        if not self.paths.marker.is_file():
            return {"message": "No managed installation"}
        disk = shutil.disk_usage(base)
        watched = ("state", "backups", "cache", "tmp", "logs", "releases")
        result: dict[str, Any] = {"root": str(base), "disk_free": disk.free,
                                  "paths": {}}
        for name in watched:
            path = base / name
            if path.is_symlink():
                result["paths"][name] = "symlink: inspect manually"
            elif path.exists():
                scan = self._run("du", "-sb", str(path), check=False, timeout=20)
                result["paths"][name] = (int(scan.stdout.split()[0]) if scan.returncode == 0
                                          and scan.stdout.split() else "unavailable")
        return result

    def cleanup_transient(self) -> dict[str, int]:
        """Only discard interrupted media work, with the service fully stopped."""
        with self._lock():
            self._managed()
            if self._active():
                raise ManagerError("Stop the bot before cleaning transient media")
            self._poller_free()
            removed: dict[str, int] = {}
            for root in (self.paths.state / "jobs", self.paths.runtime_temp):
                if root.is_symlink():
                    raise ManagerError(f"Transient path is a symlink: {root}")
                count = 0
                if root.is_dir():
                    for item in root.iterdir():
                        if item.is_dir() and not item.is_symlink():
                            shutil.rmtree(item)
                        else:
                            item.unlink()
                        count += 1
                removed[root.name] = count
            return removed

    def _init_repo(self) -> None:
        if self.paths.repo.is_symlink():
            raise ManagerError("Repository path cannot be a symlink")
        if not self.paths.repo.exists():
            self._run("git", "init", "--bare", str(self.paths.repo))
            self._run("git", "--git-dir", str(self.paths.repo), "remote", "add", "origin",
                      self.repo_url)
        remote = self._run("git", "--git-dir", str(self.paths.repo), "remote", "get-url",
                           "origin").stdout.strip()
        if remote != self.repo_url:
            raise ManagerError("Repository origin changed; refusing to fetch or execute it")

    def _fetch(self) -> None:
        self.on_event("Fetching existing version branches from GitHub…")
        self._init_repo()
        self._run("git", "--git-dir", str(self.paths.repo), "fetch", "--no-tags", "origin",
                  "+refs/heads/v*:refs/remotes/origin/v*", timeout=240)

    def versions(self, *, refresh: bool = False) -> list[dict[str, str]]:
        if refresh:
            if not self.paths.marker.is_file():
                lines = self._run("git", "ls-remote", "--heads", self.repo_url,
                                  "refs/heads/v*", timeout=240).stdout.splitlines()
                return [{"branch": branch.removeprefix("refs/heads/"), "commit": commit}
                        for line in lines if len(parts := line.split()) == 2
                        for commit, branch in [parts]
                        if branch.startswith("refs/heads/") and
                        BRANCH.fullmatch(branch[11:]) and SHA.fullmatch(commit)]
            with self._lock():
                self._managed()
                self._fetch()
        if not self.paths.repo.exists():
            return []
        lines = self._run("git", "--git-dir", str(self.paths.repo), "for-each-ref",
                          "--format=%(refname:short) %(objectname)",
                          "refs/remotes/origin/v*").stdout.splitlines()
        return [{"branch": name.removeprefix("origin/"), "commit": commit} for line in lines
                if len(parts := line.split()) == 2
                for name, commit in [parts]
                if name.startswith("origin/") and BRANCH.fullmatch(name[7:])
                and SHA.fullmatch(commit)]

    def _target(self, branch: str, commit: str | None = None) -> str:
        if not BRANCH.fullmatch(branch):
            raise ManagerError("Select an existing major version branch such as v2 or v2-test")
        ref = f"refs/remotes/origin/{branch}^{{commit}}"
        tip = self._run("git", "--git-dir", str(self.paths.repo), "rev-parse", "--verify",
                        ref, check=False)
        if tip.returncode or not SHA.fullmatch(tip.stdout.strip()):
            raise ManagerError(f"Branch {branch} is unavailable; refresh versions first")
        head = tip.stdout.strip()
        if commit is None:
            return head
        if not SHA.fullmatch(commit):
            raise ManagerError("Commit must be a full 40-character Git SHA")
        exists = self._run("git", "--git-dir", str(self.paths.repo), "cat-file", "-t", commit,
                           check=False)
        ancestry = self._run("git", "--git-dir", str(self.paths.repo), "merge-base",
                             "--is-ancestor", commit, head, check=False)
        if exists.stdout.strip() != "commit" or ancestry.returncode:
            raise ManagerError("Commit is not reachable from the selected remote branch")
        return commit

    def _stage(self, sha: str, *, checks: bool = True) -> Path:
        self.on_event(f"Preparing release {sha[:12]}…")
        release = self.paths.releases / sha
        if release.is_symlink() or self.paths.releases.is_symlink():
            raise ManagerError("Release path cannot be a symlink")
        self.paths.releases.mkdir(parents=True, exist_ok=True)
        if not release.exists():
            self._run("git", "--git-dir", str(self.paths.repo), "worktree", "add", "--detach",
                      str(release), sha, timeout=120)
        actual = self._run("git", "-C", str(release), "rev-parse", "HEAD").stdout.strip()
        if actual != sha:
            raise ManagerError("Release worktree has an unexpected commit")
        if not (release / "uv.lock").is_file():
            raise ManagerError("Selected commit has no uv.lock; refusing unpinned installation")
        self.paths.cache.mkdir(parents=True, exist_ok=True)
        self.paths.temporary.mkdir(parents=True, exist_ok=True)
        uv_env = os.environ.copy()
        uv_env["UV_CACHE_DIR"] = str(self.paths.cache / "uv")
        uv_env["XDG_CACHE_HOME"] = str(self.paths.cache)
        self.paths.manager_temp.mkdir(parents=True, exist_ok=True)
        uv_env["TMPDIR"] = str(self.paths.manager_temp)
        uv_env["UV_NO_PROGRESS"] = "1"
        self.on_event("Installing locked Python dependencies…")
        self._run(self.uv, "sync", "--locked", "--extra", "dev", cwd=release,
                  timeout=1200, env=uv_env)
        if not (release / ".venv/bin/mikachan").is_file():
            raise ManagerError("Selected release does not provide the expected bot command")
        quality: dict[str, Any] = {"commit": sha, "checked_at": time.time()}
        if checks:
            for command in (("pytest", "-q"), ("ruff", "check", ".")):
                self.on_event(f"Running {command[0]}…")
                self._run(str(release / ".venv" / "bin" / command[0]), *command[1:],
                          cwd=release, timeout=300)
                quality[command[0]] = "passed"
            self.on_event("Running mypy (advisory for downloader-owned code)…")
            typing = self._run(str(release / ".venv/bin/mypy"), "src", cwd=release,
                               timeout=300, check=False)
            quality["mypy"] = "passed" if typing.returncode == 0 else "failed"
            if typing.returncode:
                match = re.search(r"Found ([0-9]+) errors?", typing.stdout)
                quality["mypy_errors"] = int(match.group(1)) if match else None
                self.on_event("mypy has existing source errors; the quality record will show them")
        else:
            quality["checks"] = "skipped by operator"
        _atomic(release / ".manager-quality.json", json.dumps(quality, indent=2) + "\n")
        return release

    def _validate_config(self, values: dict[str, str], release: Path) -> None:
        unknown = set(values) - ENV_KEYS
        if unknown:
            raise ManagerError("Unsupported environment keys: " + ", ".join(sorted(unknown)))
        try:
            owner = int(values["OWNER_TELEGRAM_ID"])
            users = _ids(values["ALLOWED_TELEGRAM_IDS"])
            groups = _ids(values.get("ALLOWED_GROUP_IDS", ""), negative=True)
            mommy = _ids(values.get("MOMMY_TELEGRAM_IDS", ""))
        except (KeyError, ValueError) as exc:
            raise ManagerError("Owner and allowlist must contain numeric Telegram IDs") from exc
        if owner <= 0 or owner not in users or len(users) > 4 or not mommy <= users:
            raise ManagerError("Allowlist must include the owner, have at most four users, and contain Mommy IDs")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", values.get("BOT_TOKEN", "")):
            raise ManagerError("A valid Telegram bot token is required")
        if groups and values.get("PRIVATE_CHATS_ONLY", "true").lower() != "false":
            raise ManagerError("Set PRIVATE_CHATS_ONLY=false to enable listed groups")
        if values.get("DATA_DIR") != str(self.paths.state):
            raise ManagerError("DATA_DIR must be the managed persistent state directory")
        # Validate all runtime constraints with the exact code being activated.
        self.paths.temporary.mkdir(parents=True, exist_ok=True)
        self.paths.manager_temp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.paths.manager_temp) as directory:
            candidate = Path(directory) / "config.env"
            _atomic(candidate, _render_env(values))
            code = ("import sys; from mikachan.config import Settings; "
                    "Settings(_env_file=sys.argv[1]); print('valid')")
            clean = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
            result = self._run(str(release / ".venv/bin/python"), "-c", code,
                               str(candidate), cwd=release, env=clean, check=False)
            if result.returncode:
                # Pydantic may echo input values in its error. Never surface that output.
                raise ManagerError("Runtime settings rejected the configuration; check IDs and limits")

    def _create_user(self) -> None:
        try:
            grp.getgrnam("mikachan")
        except KeyError:
            self._run("groupadd", "--system", "mikachan")
        try:
            pwd.getpwnam("mikachan")
        except KeyError:
            self._run("useradd", "--system", "--home-dir", str(self.paths.state),
                      "--shell", "/usr/sbin/nologin", "--gid", "mikachan",
                      "mikachan")

    def _set_file_group(self, path: Path) -> None:
        if self.require_root:
            os.chown(path, 0, grp.getgrnam("mikachan").gr_gid)
        os.chmod(path, 0o640)

    def _state_dirs(self) -> None:
        if any(path.is_symlink() for path in (self.paths.state, self.paths.backups,
                                              self.paths.temporary, self.paths.runtime_temp,
                                              self.paths.manager_temp, self.paths.cache,
                                              self.paths.logs, self.paths.env.parent)):
            raise ManagerError("Managed paths cannot be symlinks")
        self.paths.state.mkdir(parents=True, exist_ok=True)
        self.paths.backups.mkdir(parents=True, exist_ok=True)
        self.paths.temporary.mkdir(parents=True, exist_ok=True)
        self.paths.runtime_temp.mkdir(parents=True, exist_ok=True)
        self.paths.manager_temp.mkdir(parents=True, exist_ok=True)
        self.paths.cache.mkdir(parents=True, exist_ok=True)
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        self.paths.env.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.paths.backups, 0o700)
        os.chmod(self.paths.state, 0o700)
        os.chmod(self.paths.temporary, 0o750)
        os.chmod(self.paths.runtime_temp, 0o700)
        os.chmod(self.paths.manager_temp, 0o700)
        os.chmod(self.paths.cache, 0o700)
        os.chmod(self.paths.logs, 0o700)
        os.chmod(self.paths.env.parent, 0o750)
        if self.require_root:
            user = pwd.getpwnam("mikachan")
            group = grp.getgrnam("mikachan").gr_gid
            for path in (self.paths.state, self.paths.runtime_temp, self.paths.cache,
                         self.paths.logs):
                os.chown(path, user.pw_uid, group)
            os.chown(self.paths.temporary, 0, group)
            os.chown(self.paths.env.parent, 0, group)

    def _unit_text(self) -> str:
        p = self.paths
        return ("# Managed by Mikachan panel\n"
                "[Unit]\nDescription=Mikachan private Telegram bot\n"
                "After=network-online.target\nWants=network-online.target\n\n"
                "[Service]\nType=simple\nUser=mikachan\nGroup=mikachan\n"
                f"WorkingDirectory={p.current}\nEnvironmentFile={p.env}\n"
                f"Environment=PATH={p.current}/.venv/bin:/usr/local/bin:/usr/bin:/bin\n"
                f"Environment=HOME={p.state}\n"
                f"Environment=TMPDIR={p.runtime_temp}\n"
                f"Environment=XDG_CACHE_HOME={p.cache}\n"
                f"ExecStart=/usr/bin/flock -n {p.state}/polling.lock "
                f"{p.current}/.venv/bin/mikachan run\n"
                "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=60\nUMask=0077\n"
                "NoNewPrivileges=true\nProtectSystem=strict\nProtectHome=true\n"
                f"ReadWritePaths={p.state} {p.runtime_temp} {p.cache} {p.logs}\n"
                "PrivateTmp=true\n\n"
                "[Install]\nWantedBy=multi-user.target\n")

    def _switch_link(self, sha: str) -> None:
        target = self.paths.releases / sha
        if target.is_symlink() or not target.is_dir():
            raise ManagerError("Target release is missing")
        self._grant_release_access(target)
        temporary = self.paths.base / (".current-" + secrets.token_hex(4))
        temporary.symlink_to(target, target_is_directory=True)
        try:
            os.replace(temporary, self.paths.current)
        finally:
            temporary.unlink(missing_ok=True)

    def _grant_release_access(self, release: Path) -> None:
        """Let the unprivileged service read its release under a private panel umask."""
        group = grp.getgrnam("mikachan").gr_gid if self.require_root else None
        for entry in (self.paths.base, self.paths.releases):
            if entry.is_symlink():
                raise ManagerError(f"Managed path is a symlink: {entry}")
            if group is not None:
                os.chown(entry, -1, group)
            os.chmod(entry, stat.S_IMODE(entry.stat().st_mode) | stat.S_IRGRP | stat.S_IXGRP)
        for root, directories, files in os.walk(release, followlinks=False):
            for entry in (Path(root), *(Path(root) / name for name in directories + files)):
                if entry.is_symlink():
                    continue
                info = entry.lstat()
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise ManagerError(f"Unexpected release entry: {entry}")
                if group is not None:
                    os.chown(entry, -1, group)
                permissions = stat.S_IMODE(info.st_mode) | stat.S_IRGRP
                if stat.S_ISDIR(info.st_mode) or info.st_mode & 0o111:
                    permissions |= stat.S_IXGRP
                os.chmod(entry, permissions)

    def _snapshot(self, *, reason: str) -> str:
        if self.paths.state.is_symlink() or self.paths.backups.is_symlink():
            raise ManagerError("State and backup paths cannot be symlinks")
        self.paths.backups.mkdir(parents=True, exist_ok=True)
        snapshot = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4)
        directory = self.paths.backups / snapshot
        directory.mkdir(mode=0o700)
        database = self.paths.state / "mikachan.sqlite"
        if database.is_symlink():
            raise ManagerError("Database path cannot be a symlink")
        if database.exists():
            source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                destination = sqlite3.connect(directory / "mikachan.sqlite")
                try:
                    source.backup(destination)
                    check = destination.execute("PRAGMA integrity_check").fetchone()
                    if check is None or check[0] != "ok":
                        raise ManagerError("SQLite backup integrity check failed")
                finally:
                    destination.close()
            finally:
                source.close()
            os.chmod(directory / "mikachan.sqlite", 0o600)
        if self.paths.env.exists():
            shutil.copy2(self.paths.env, directory / "config.env")
            os.chmod(directory / "config.env", 0o600)
        _atomic(directory / "metadata.json", json.dumps({"reason": reason, "created":
                time.time(), "database": database.exists()}, indent=2) + "\n")
        return snapshot

    def _restore_snapshot(self, snapshot: str) -> None:
        if self.paths.state.is_symlink() or self.paths.backups.is_symlink():
            raise ManagerError("State and backup paths cannot be symlinks")
        if not SNAPSHOT.fullmatch(snapshot):
            raise ManagerError("Invalid snapshot ID")
        directory = self.paths.backups / snapshot
        if directory.is_symlink() or not (directory / "metadata.json").is_file():
            raise ManagerError("Snapshot does not exist")
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        database = self.paths.state / "mikachan.sqlite"
        if metadata["database"]:
            source = directory / "mikachan.sqlite"
            if source.is_symlink() or not source.is_file():
                raise ManagerError("Snapshot database is missing")
            with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ManagerError("Snapshot database failed integrity check")
            temporary = database.with_suffix(".restoring")
            shutil.copy2(source, temporary)
            if self.require_root:
                user = pwd.getpwnam("mikachan")
                os.chown(temporary, user.pw_uid, user.pw_gid)
            os.chmod(temporary, 0o600)
            for suffix in ("-wal", "-shm"):
                Path(str(database) + suffix).unlink(missing_ok=True)
            os.replace(temporary, database)
        else:
            for suffix in ("", "-wal", "-shm"):
                Path(str(database) + suffix).unlink(missing_ok=True)
        config = directory / "config.env"
        if config.is_file():
            _atomic(self.paths.env, config.read_text(encoding="utf-8"), 0o640)
            self._set_file_group(self.paths.env)

    def _healthy(self) -> bool:
        # Active means the process survived startup; live Telegram acceptance is separate.
        for _ in range(3):
            if not self._active():
                return False
            time.sleep(1)
        return True

    def _poller_free(self) -> None:
        lock = self.paths.state / "polling.lock"
        if lock.is_symlink():
            raise ManagerError("Polling lock path cannot be a symlink")
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "a+") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ManagerError("Polling lock must be a single regular file")
            if self.require_root:
                user = pwd.getpwnam("mikachan")
                os.fchown(file.fileno(), user.pw_uid,
                          grp.getgrnam("mikachan").gr_gid)
            os.fchmod(file.fileno(), 0o600)
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ManagerError("Another local bot poller still owns the state directory") from exc
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    def _stop_and_snapshot(self, reason: str) -> tuple[str, bool]:
        was_active = self._active()
        self._systemctl("stop")
        try:
            self._poller_free()
            return self._snapshot(reason=reason), was_active
        except BaseException:
            if was_active:
                self._systemctl("start", check=False)
            raise

    def _activate(self, manifest: dict[str, Any], branch: str, sha: str) -> str:
        self._ensure_consistent(manifest)
        old = manifest["history"][-1]
        if old["sha"] == sha:
            return "Already at the selected commit"
        self.on_event("Stopping service and saving consistent SQLite snapshot…")
        snapshot, was_active = self._stop_and_snapshot(reason=f"before {branch}@{sha[:12]}")
        try:
            _atomic(self.paths.journal, json.dumps({"old": old, "target": {"branch": branch,
                    "sha": sha}, "snapshot": snapshot, "was_active": was_active}) + "\n")
            self.on_event("Switching release and checking service health…")
            self._switch_link(sha)
            if was_active:
                self._systemctl("start")
            if was_active and not self._healthy():
                raise ManagerError("Service did not stay active after activation")
            manifest["history"].append({"branch": branch, "sha": sha,
                                        "at": time.time(), "snapshot_before": snapshot})
            self._save_manifest(manifest)
        except BaseException:
            self.on_event("Activation failed; restoring previous release and snapshot…")
            self._systemctl("stop", check=False)
            self._restore_snapshot(snapshot)
            self._switch_link(old["sha"])
            if was_active:
                self._systemctl("start", check=False)
            self.paths.journal.unlink(missing_ok=True)
            raise
        else:
            self.paths.journal.unlink(missing_ok=True)
        try:
            self._refresh_panel(self.paths.releases / sha)
        except (OSError, ValueError) as exc:
            self.on_event(f"Panel refresh skipped: {type(exc).__name__}")
        return f"Activated {branch}@{sha[:12]}; backup {snapshot}"

    def _refresh_panel(self, release: Path) -> None:
        """Refresh only bootstrap-owned panel files after a verified activation."""
        panel = self.paths.base / "panel"
        directory = panel / "deploy"
        marker = panel / ".managed-by-mikachan"
        if not marker.is_file():
            return
        if panel.is_symlink() or directory.is_symlink() or marker.is_symlink() or \
                marker.read_text(encoding="utf-8") != "Managed by Mikachan bootstrap\n":
            raise ValueError("Panel ownership marker is invalid")
        sources = []
        for name in ("__init__.py", "manager.py", "manager_ui.py",
                     "manager-requirements.txt", "manage.sh"):
            source = release / "deploy" / name
            target = directory / name
            if not source.is_file() or source.is_symlink() or target.is_symlink() or \
                    source.stat().st_size > 500_000:
                raise ValueError("Panel source or target is unsafe")
            sources.append((name, target, source.read_text(encoding="utf-8")))
        for name, target, content in sources:
            _atomic(target, content,
                    0o755 if name == "manage.sh" else 0o644)

    def install(self, config: dict[str, str] | None = None, *, branch: str = "v2",
                commit: str | None = None, checks: bool = True,
                reuse_existing_config: bool = False) -> str:
        if self.require_root:
            self._root()
            self._require_host()
        with self._lock():
            self.on_event("Checking managed installation boundaries…")
            if self.paths.manifest.exists():
                raise ManagerError("Already installed; use update or switch")
            if self.paths.base.is_dir() and not self.paths.marker.exists() and \
                    any(item.name != ".manager.lock" for item in self.paths.base.iterdir()):
                raise ManagerError("Existing unmanaged files found; refusing to overwrite them")
            if self.paths.unit.exists():
                raise ManagerError("Existing systemd unit found; inspect or migrate it first")
            if self.paths.marker.exists() and self.paths.marker.read_text() != MANAGED:
                raise ManagerError("Unknown manager marker; refusing installation")
            _atomic(self.paths.marker, MANAGED)
            self._fetch()
            sha = self._target(branch, commit)
            release = self._stage(sha, checks=checks)
            try:
                self._refresh_panel(release)
            except (OSError, ValueError) as exc:
                self.on_event(f"Panel refresh skipped: {type(exc).__name__}")
            values = dict(config) if config is not None else (
                _parse_env(self.paths.env) if reuse_existing_config and self.paths.env.is_file()
                else None)
            if values is not None:
                values["DATA_DIR"] = str(self.paths.state)
                self._validate_config(values, release)
            self._create_user()
            self._state_dirs()
            self._poller_free()
            if values is not None:
                _atomic(self.paths.env, _render_env(values), 0o640)
                self._set_file_group(self.paths.env)
            self._switch_link(sha)
            _atomic(self.paths.unit, self._unit_text(), 0o644)
            self._run("systemctl", "daemon-reload")
            self._save_manifest({"schema": 1, "history": [{"branch": branch, "sha": sha,
                                  "at": time.time(), "snapshot_before": None}]})
            if values is None:
                return f"Installed {branch}@{sha[:12]}; open Setup to connect your bot"
            self._systemctl("enable")
            self._systemctl("start")
            self.on_event("Checking service health…")
            if not self._healthy():
                self._systemctl("stop", check=False)
                raise ManagerError("Installation staged, but the service did not stay active; inspect logs")
            return f"Installed {branch}@{sha[:12]}; service active (Telegram unverified)"

    def setup(self, config: dict[str, str]) -> str:
        """Apply first or repeated setup without discarding existing bot data."""
        with self._lock():
            manifest = self._manifest()
            self._ensure_consistent(manifest)
            old = _parse_env(self.paths.env) if self.paths.env.exists() else {}
            values = dict(old)
            values.update(config)
            values["ALLOWED_TELEGRAM_IDS"] = config.get("ALLOWED_TELEGRAM_IDS") or \
                config["OWNER_TELEGRAM_ID"]
            values["ALLOWED_GROUP_IDS"] = config.get("ALLOWED_GROUP_IDS", "")
            if values.get("MOMMY_TELEGRAM_IDS"):
                retained = _ids(values["MOMMY_TELEGRAM_IDS"]) & \
                    _ids(values["ALLOWED_TELEGRAM_IDS"])
                values["MOMMY_TELEGRAM_IDS"] = ",".join(map(str, sorted(retained)))
            values["PRIVATE_CHATS_ONLY"] = "false" if values["ALLOWED_GROUP_IDS"] else "true"
            values["DATA_DIR"] = str(self.paths.state)
            self._validate_config(values, self.paths.current)
            undo, was_active = self._stop_and_snapshot(reason="before setup")
            try:
                _atomic(self.paths.env, _render_env(values), 0o640)
                self._set_file_group(self.paths.env)
                if (self.paths.state / "mikachan.sqlite").exists():
                    if old.get("OWNER_TELEGRAM_ID") != values["OWNER_TELEGRAM_ID"]:
                        self._sync_access("OWNER_TELEGRAM_ID", values, old)
                    else:
                        self._sync_access("ALLOWED_TELEGRAM_IDS", values, old)
                    self._sync_access("ALLOWED_GROUP_IDS", values, old)
                self._systemctl("enable")
                self._systemctl("start")
                if not self._healthy():
                    raise ManagerError("Bot service did not stay active after setup")
            except BaseException:
                self._systemctl("stop", check=False)
                self._restore_snapshot(undo)
                if not old:
                    self.paths.env.unlink(missing_ok=True)
                if was_active:
                    self._systemctl("start", check=False)
                raise
            return f"Setup complete; service active. Previous state snapshot: {undo}"

    def update(self, *, checks: bool = True) -> str:
        with self._lock():
            manifest = self._manifest()
            self._ensure_consistent(manifest)
            current = manifest["history"][-1]
            self._fetch()
            sha = self._target(current["branch"])
            if sha == current["sha"]:
                return "Already at the latest commit on " + current["branch"]
            ancestor = self._run("git", "--git-dir", str(self.paths.repo), "merge-base",
                                 "--is-ancestor", current["sha"], sha, check=False)
            if ancestor.returncode:
                raise ManagerError("Remote branch diverged; use an explicit reviewed switch")
            self._stage(sha, checks=checks)
            if self.paths.env.exists():
                self._validate_config(_parse_env(self.paths.env), self.paths.releases / sha)
            return self._activate(manifest, current["branch"], sha)

    def switch(self, branch: str, *, commit: str | None = None, checks: bool = True) -> str:
        with self._lock():
            manifest = self._manifest()
            self._ensure_consistent(manifest)
            self._fetch()
            sha = self._target(branch, commit)
            self._stage(sha, checks=checks)
            if self.paths.env.exists():
                self._validate_config(_parse_env(self.paths.env), self.paths.releases / sha)
            return self._activate(manifest, branch, sha)

    def rollback(self) -> str:
        with self._lock():
            manifest = self._manifest()
            self._ensure_consistent(manifest)
            if len(manifest["history"]) < 2:
                raise ManagerError("No previous installed release exists")
            previous = manifest["history"][-2]
            current = manifest["history"][-1]
            snapshot = current["snapshot_before"]
            if not snapshot:
                raise ManagerError("Previous database snapshot is missing")
            redo, was_active = self._stop_and_snapshot(reason="before rollback")
            try:
                _atomic(self.paths.journal, json.dumps({"old": current, "target": previous,
                    "snapshot": redo, "was_active": was_active}) + "\n")
                self._restore_snapshot(snapshot)
                self._switch_link(previous["sha"])
                if was_active:
                    self._systemctl("start")
                if was_active and not self._healthy():
                    raise ManagerError("Previous release did not stay active")
                manifest["history"].append({"branch": previous["branch"],
                    "sha": previous["sha"], "at": time.time(), "snapshot_before": redo})
                self._save_manifest(manifest)
            except BaseException:
                self._systemctl("stop", check=False)
                self._restore_snapshot(redo)
                self._switch_link(current["sha"])
                if was_active:
                    self._systemctl("start", check=False)
                self.paths.journal.unlink(missing_ok=True)
                raise
            else:
                self.paths.journal.unlink(missing_ok=True)
            return f"Restored {previous['branch']}@{previous['sha'][:12]} and snapshot {snapshot}; " \
                   "later database changes were discarded"

    def recover(self) -> str:
        with self._lock():
            manifest = self._manifest()
            if not self.paths.journal.exists():
                return "No interrupted activation"
            record = json.loads(self.paths.journal.read_text(encoding="utf-8"))
            old = record["old"]
            if old["sha"] != manifest["history"][-1]["sha"]:
                target = record["target"]
                if target["sha"] == manifest["history"][-1]["sha"] and \
                        self.paths.current.resolve() == self.paths.releases / target["sha"] \
                        and (self._active() or not record.get("was_active", True)):
                    self.paths.journal.unlink()
                    return "Activation was already committed and service is active; journal cleared"
                raise ManagerError("Journal and manifest disagree; manual inspection required")
            self._systemctl("stop", check=False)
            self._restore_snapshot(record["snapshot"])
            self._switch_link(old["sha"])
            if record.get("was_active", True):
                self._systemctl("start", check=False)
            self.paths.journal.unlink()
            return f"Restored interrupted activation to {old['branch']}@{old['sha'][:12]}"

    def backup(self) -> str:
        with self._lock():
            self._manifest()
            return self._snapshot(reason="manual")

    def backups(self) -> list[str]:
        if not self.paths.backups.exists():
            return []
        return sorted((item.name for item in self.paths.backups.iterdir()
                       if item.is_dir() and not item.is_symlink() and SNAPSHOT.fullmatch(item.name)
                       and (item / "metadata.json").is_file()), reverse=True)

    def prune_releases(self, *, keep: int = 3, apply: bool = False) -> list[str]:
        """Prune old worktrees, never the current or previous installed release."""
        if not 2 <= keep <= 20:
            raise ManagerError("Keep between 2 and 20 recent distinct releases")
        with self._lock():
            manifest = self._manifest()
            self._ensure_consistent(manifest)
            retained: set[str] = set()
            for event in reversed(manifest["history"]):
                if len(retained) >= keep:
                    break
                retained.add(event["sha"])
            releases = [item for item in self.paths.releases.iterdir() if item.is_dir()
                        and not item.is_symlink() and SHA.fullmatch(item.name)
                        and item.name not in retained]
            names = sorted(item.name for item in releases)
            if apply:
                for release in releases:
                    actual = self._run("git", "-C", str(release), "rev-parse", "HEAD",
                                       check=False)
                    if actual.returncode or actual.stdout.strip() != release.name:
                        raise ManagerError(f"Unrecognized worktree {release.name}; inspect manually")
                    self._run("git", "--git-dir", str(self.paths.repo), "worktree",
                              "remove", "--force", str(release), timeout=120)
            return names

    def restore(self, snapshot: str) -> str:
        with self._lock():
            self._manifest()
            undo, was_active = self._stop_and_snapshot(reason=f"before restore {snapshot}")
            try:
                self._restore_snapshot(snapshot)
                if was_active:
                    self._systemctl("start")
                if was_active and not self._healthy():
                    raise ManagerError("Service did not stay active after snapshot restore")
            except BaseException:
                self._systemctl("stop", check=False)
                self._restore_snapshot(undo)
                if was_active:
                    self._systemctl("start", check=False)
                raise
            return f"Restored snapshot {snapshot}; previous state preserved as {undo}"

    def config(self) -> dict[str, str]:
        self._manifest()
        if not self.paths.env.exists():
            return {"SETUP": "required"}
        values = _parse_env(self.paths.env)
        return {key: ("(set)" if key in PRIVATE_KEYS and values.get(key) else
                      values.get(key, "")) for key in sorted(ENV_KEYS)}

    def set_config(self, key: str, value: str) -> str:
        if key not in EDITABLE:
            raise ManagerError("This setting cannot be changed through the panel")
        with self._lock():
            manifest = self._manifest()
            self._ensure_consistent(manifest)
            if not self.paths.env.exists():
                raise ManagerError("Run Setup before editing individual settings")
            values = _parse_env(self.paths.env)
            previous = dict(values)
            if key == "OWNER_TELEGRAM_ID":
                try:
                    users = _ids(values["ALLOWED_TELEGRAM_IDS"])
                    users.discard(int(values["OWNER_TELEGRAM_ID"]))
                    users.add(int(value))
                except ValueError as exc:
                    raise ManagerError("Owner must be a numeric Telegram ID") from exc
                values["ALLOWED_TELEGRAM_IDS"] = ",".join(map(str, sorted(users)))
            if key == "ALLOWED_GROUP_IDS":
                values["PRIVATE_CHATS_ONLY"] = "false" if value else "true"
            if value or key in {"ALLOWED_GROUP_IDS", "MOMMY_TELEGRAM_IDS"}:
                values[key] = value
            else:
                values.pop(key, None)
            self._validate_config(values, self.paths.current)
            undo, was_active = self._stop_and_snapshot(reason=f"before setting {key}")
            try:
                _atomic(self.paths.env, _render_env(values), 0o640)
                self._set_file_group(self.paths.env)
                if key in {"ALLOWED_TELEGRAM_IDS", "ALLOWED_GROUP_IDS", "OWNER_TELEGRAM_ID"}:
                    self._sync_access(key, values, previous)
                if was_active:
                    self._systemctl("start")
                if was_active and not self._healthy():
                    raise ManagerError("Service did not stay active after setting change")
            except BaseException:
                self._systemctl("stop", check=False)
                self._restore_snapshot(undo)
                if was_active:
                    self._systemctl("start", check=False)
                raise
            return f"Updated {key}; previous configuration and database: {undo}"

    def _sync_access(self, key: str, values: dict[str, str],
                     previous: dict[str, str] | None = None) -> None:
        database = self.paths.state / "mikachan.sqlite"
        if not database.is_file():
            raise ManagerError("Bot database is missing; cannot synchronize authorization")
        users = _ids(values["ALLOWED_TELEGRAM_IDS"])
        groups = _ids(values.get("ALLOWED_GROUP_IDS", ""), negative=True)
        with sqlite3.connect(database) as db:
            unified = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                      "AND name='access_bootstrap'").fetchone())
            existing_users = {row[0] for row in db.execute("SELECT user_id FROM access")}
            existing_groups = {row[0] for row in db.execute("SELECT chat_id FROM allowed_groups")}
            removed_users: set[int] = set()
            removed_groups: set[int] = set()
            if key == "OWNER_TELEGRAM_ID" and unified:
                protected = db.execute("SELECT primary_owner_id FROM access_bootstrap "
                                       "WHERE singleton=1").fetchone()
                if not protected:
                    raise ManagerError("Protected Owner registry is inconsistent")
                old_owner, new_owner = int(protected[0]), int(values["OWNER_TELEGRAM_ID"])
                if old_owner != new_owner:
                    now = time.time()
                    db.execute("INSERT OR IGNORE INTO access VALUES (?)", (new_owner,))
                    db.execute("INSERT INTO people VALUES (?,?,'OWNER','ACTIVE',NULL,'',?,?) "
                        "ON CONFLICT(user_id) DO UPDATE SET role='OWNER',status='ACTIVE',"
                        "suspended_until=NULL,reason='',updated=excluded.updated",
                        (new_owner, f"Owner {new_owner}", now, now))
                    db.execute("UPDATE people SET role='USER',status='REVOKED',updated=? "
                               "WHERE user_id=?", (now, old_owner))
                    db.execute("DELETE FROM access WHERE user_id=?", (old_owner,))
                    db.execute("DELETE FROM admin_grants WHERE user_id=?", (old_owner,))
                    db.execute("UPDATE access_bootstrap SET primary_owner_id=? WHERE singleton=1",
                               (new_owner,))
                    db.execute("INSERT INTO access_audit "
                        "(created,actor_id,action,user_id,detail) VALUES (?,0,'operator_owner_change',?,?)",
                        (now, new_owner, f"replaced {old_owner}"))
                    removed_users.add(old_owner)
                key = "ALLOWED_TELEGRAM_IDS"
            elif key == "OWNER_TELEGRAM_ID":
                key = "ALLOWED_TELEGRAM_IDS"
            if unified and previous is not None:
                prior_users = _ids(previous.get("ALLOWED_TELEGRAM_IDS", ""))
                prior_groups = _ids(previous.get("ALLOWED_GROUP_IDS", ""), negative=True)
            else:
                prior_users, prior_groups = existing_users, existing_groups
            if key == "ALLOWED_TELEGRAM_IDS":
                additions = users - prior_users
                db.executemany("INSERT OR IGNORE INTO access VALUES (?)", ((x,) for x in additions))
                removed_users |= prior_users - users
                db.executemany("DELETE FROM access WHERE user_id=?",
                               ((x,) for x in removed_users))
                if unified:
                    now = time.time()
                    db.executemany("INSERT OR IGNORE INTO people VALUES "
                        "(?,?,'USER','ACTIVE',NULL,'',?,?)",
                        ((x, f"Operator user {x}", now, now) for x in additions))
                    db.executemany("UPDATE people SET status='REVOKED',updated=? "
                                   "WHERE user_id=? AND role!='OWNER'",
                                   ((now, x) for x in removed_users))
            if key == "ALLOWED_GROUP_IDS":
                additions = groups - prior_groups
                db.executemany("INSERT OR IGNORE INTO allowed_groups VALUES (?)", ((x,) for x in additions))
                removed_groups = prior_groups - groups
                db.executemany("DELETE FROM allowed_groups WHERE chat_id=?",
                               ((x,) for x in removed_groups))
                if unified:
                    now = time.time()
                    db.executemany("INSERT OR IGNORE INTO group_registry VALUES "
                        "(?,?,'ACTIVE',?,?)", ((x, f"Operator group {x}", now, now)
                                                for x in additions))
                    db.executemany("UPDATE group_registry SET status='REVOKED',updated=? "
                                   "WHERE chat_id=?", ((now, x) for x in removed_groups))
                db.execute("INSERT OR IGNORE INTO group_access_seeded VALUES (1)")
            for column, removed in (("user_id", removed_users), ("chat_id", removed_groups)):
                for ident in removed:
                    db.execute(f"UPDATE jobs SET state='CANCELLED', updated=? WHERE {column}=? "
                               "AND state IN ('QUEUED','RESOLVING','DOWNLOADING','VERIFYING')",
                               (time.time(), ident))

    def owner_approvals(self) -> list[dict[str, Any]]:
        """Read pending Telegram requests through the independent VPS operator channel."""
        self._root()
        database = self.paths.state / "mikachan.sqlite"
        if not database.is_file():
            return []
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='owner_requests'").fetchone():
                return []
            rows = db.execute("SELECT token,requester_id,target_id,action,expires "
                "FROM owner_requests WHERE status='PENDING' AND expires>? "
                "ORDER BY created DESC LIMIT 20", (time.time(),)).fetchall()
        return [dict(zip(("token", "requester_id", "target_id", "action", "expires"), row))
                for row in rows]

    def approve_owner(self, token: str, target: int) -> str:
        """Apply a one-time Owner request after OS-root authentication and backup."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}", token) or not 0 < target < 2**63:
            raise ManagerError("Invalid Owner approval token or target ID")
        with self._lock():
            self._ensure_consistent(self._manifest())
            undo, was_active = self._stop_and_snapshot(reason="before Owner approval")
            try:
                database = self.paths.state / "mikachan.sqlite"
                with sqlite3.connect(database) as db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute("SELECT requester_id,target_id,action FROM owner_requests "
                        "WHERE token=? AND target_id=? AND status='PENDING' AND expires>?",
                        (token, target, time.time())).fetchone()
                    primary = db.execute("SELECT primary_owner_id FROM access_bootstrap "
                                         "WHERE singleton=1").fetchone()
                    person = db.execute("SELECT role,status FROM people WHERE user_id=?",
                                        (target,)).fetchone()
                    if not row or not primary or row[0] != primary[0] or not person or \
                            person[1] != "ACTIVE" or target == primary[0]:
                        raise ManagerError("Request expired, changed or not initiated by the primary Owner")
                    if row[2] == "PROMOTE" and person[0] not in {"USER", "ADMIN"} or \
                            row[2] == "REVOKE" and person[0] != "OWNER":
                        raise ManagerError("Target role changed since the request")
                    now = time.time()
                    if row[2] == "PROMOTE":
                        db.execute("UPDATE people SET role='OWNER',updated=? WHERE user_id=?",
                                   (now, target))
                        db.execute("INSERT OR IGNORE INTO access VALUES (?)", (target,))
                    else:
                        db.execute("UPDATE people SET role='USER',status='REVOKED',updated=? "
                                   "WHERE user_id=?", (now, target))
                        db.execute("DELETE FROM access WHERE user_id=?", (target,))
                        db.execute("DELETE FROM admin_grants WHERE user_id=?", (target,))
                        db.execute("UPDATE jobs SET state='CANCELLED',updated=? WHERE user_id=? "
                                   "AND state IN ('QUEUED','RESOLVING','DOWNLOADING','VERIFYING')",
                                   (now, target))
                    db.execute("UPDATE owner_requests SET status='APPROVED' WHERE token=?", (token,))
                    db.execute("INSERT INTO access_audit "
                        "(created,actor_id,action,user_id,detail) VALUES (?,0,?,?,?)",
                        (now, "operator_owner_" + row[2].lower(), target,
                         f"primary requester {row[0]}"))
                if was_active:
                    self._systemctl("start")
                    if not self._healthy():
                        raise ManagerError("Service did not stay active after Owner approval")
            except BaseException:
                self._systemctl("stop", check=False)
                self._restore_snapshot(undo)
                if was_active:
                    self._systemctl("start", check=False)
                raise
            return f"Owner {row[2].lower()} approved for {target}; prior state backup: {undo}"

    def service(self, action: str) -> str:
        if action not in {"start", "stop", "restart"}:
            raise ManagerError("Unknown service action")
        with self._lock():
            self._manifest()
            if action in {"start", "restart"} and not self.paths.env.exists():
                raise ManagerError("Run Setup before starting the bot")
            self._systemctl(action)
            return f"Service {action}: {'active' if self._active() else 'inactive'}"

    def logs(self, lines: int = 80) -> str:
        if not 1 <= lines <= 200:
            raise ManagerError("Log lines must be 1–200")
        return self._run("journalctl", "-u", "mikachan.service", "-n", str(lines),
                         "--no-pager", "--output=short-iso", check=False).stdout

    def diagnostics(self, *, live: bool = False) -> dict[str, Any]:
        status = self.status()
        result: dict[str, Any] = {"installation": status, "host": self.preflight()}
        if not status["installed"] or not status.get("consistent"):
            return result
        if not status.get("config"):
            result["setup"] = "required"
            return result
        values = _parse_env(self.paths.env)
        environment = os.environ.copy()
        environment.update(values)
        doctor = self._run(str(self.paths.current / ".venv/bin/mikachan"), "doctor",
                           cwd=self.paths.current, check=False, env=environment, timeout=30)
        result["doctor"] = doctor.stdout.strip() if doctor.returncode == 0 else "doctor failed"
        database = self.paths.state / "mikachan.sqlite"
        if database.is_file():
            try:
                with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
                    result["sqlite"] = db.execute("PRAGMA quick_check").fetchone()[0]
            except sqlite3.Error:
                result["sqlite"] = "unreadable"
        if live:
            # Never include the token or request URL in an error or log.
            base = values.get("LOCAL_BOT_API_URL", "").rstrip("/") if \
                values.get("BOT_API_MODE") == "local" else "https://api.telegram.org"
            if not base:
                result["telegram"] = "local Bot API URL missing"
            else:
                token = values.get("BOT_TOKEN", "")
                try:
                    request = urllib.request.Request(
                        f"{base}/bot{token}/getMe", method="POST")
                    with urllib.request.urlopen(request, timeout=8) as response:
                        payload = json.load(response)
                    result["telegram"] = ("verified @" + payload["result"]["username"]
                                          if payload.get("ok") else "Bot API rejected identity")
                except (OSError, KeyError, ValueError, urllib.error.URLError):
                    result["telegram"] = "unavailable or rejected; inspect network and token privately"
        return result

    def _remove_code_files(self) -> None:
        """Leave panel, configuration, database and backups available for reinstall."""
        self.paths.current.unlink(missing_ok=True)
        for directory in (self.paths.releases, self.paths.repo, self.paths.cache / "uv"):
            if directory.is_symlink():
                raise ManagerError(f"Managed path is a symlink: {directory}")
            if directory.is_dir():
                shutil.rmtree(directory)
        self.paths.manifest.unlink(missing_ok=True)
        self.paths.journal.unlink(missing_ok=True)

    def remove(self, *, purge: bool = False) -> str:
        with self._lock():
            manifest = self._manifest()
            self._ensure_consistent(manifest)
            if not self.paths.unit.is_file() or \
                    not self.paths.unit.read_text(encoding="utf-8").startswith(
                        "# Managed by Mikachan panel\n"):
                raise ManagerError("Service unit is not panel-managed; refusing removal")
            if purge and (self.paths.state.is_symlink() or self.paths.backups.is_symlink()):
                raise ManagerError("State or backup path is a symlink; manual cleanup required")
            self._systemctl("stop")
            self._poller_free()
            self._systemctl("disable")
            self.paths.unit.unlink()
            self._run("systemctl", "daemon-reload")
            # Recheck immediately before recursive deletion. Only manager-owned roots are touched.
            self._managed()
            self._remove_code_files()
            if purge:
                if self.paths == Paths():
                    launcher = Path("/usr/local/bin/mikachan")
                    if launcher.is_file() and not launcher.is_symlink() and \
                            "/opt/Mikachan/panel/deploy/manage.sh" in \
                            launcher.read_text(encoding="utf-8"):
                        launcher.unlink()
                shutil.rmtree(self.paths.base)
                return "Removed managed code, service, configuration, database and backups"
            return "Removed managed code and service; configuration, database and backups retained"

    def abort_incomplete_install(self) -> str:
        """Clear a panel-created but uncommitted install without touching state."""
        with self._lock():
            self._managed()
            if self.paths.manifest.exists():
                raise ManagerError("Installation is complete; use remove instead")
            if self.paths.unit.exists():
                if not self.paths.unit.read_text(encoding="utf-8").startswith(
                        "# Managed by Mikachan panel\n"):
                    raise ManagerError("Unit is not panel-managed; inspect it manually")
                self._systemctl("stop", check=False)
                self._poller_free()
                self._systemctl("disable", check=False)
                self.paths.unit.unlink()
                self._run("systemctl", "daemon-reload", check=False)
            self._remove_code_files()
            return "Cleared incomplete managed install; state, config and backups retained"


def _cli() -> None:
    parser = argparse.ArgumentParser(prog="mikachan-manager",
        description="Manage one systemd Mikachan installation. Secrets are never CLI arguments.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("preflight")
    sub.add_parser("deps")
    versions = sub.add_parser("versions")
    versions.add_argument("--refresh", action="store_true")
    install = sub.add_parser("install")
    install.add_argument("--config", type=Path, help="0600 environment file; otherwise prompt")
    install.add_argument("--code-only", action="store_true", help="stage code; configure in Setup")
    install.add_argument("--branch", help="Required explicit version (v2, v2-test, ...)")
    install.add_argument("--commit", help="Optional full SHA reachable from that version")
    install.add_argument("--skip-checks", action="store_true")
    update = sub.add_parser("update")
    update.add_argument("--skip-checks", action="store_true")
    switch = sub.add_parser("switch")
    switch.add_argument("branch")
    switch.add_argument("--commit")
    switch.add_argument("--skip-checks", action="store_true")
    sub.add_parser("rollback")
    sub.add_parser("recover")
    sub.add_parser("backup")
    sub.add_parser("backups")
    sub.add_parser("queue")
    sub.add_parser("storage")
    sub.add_parser("cleanup")
    prune = sub.add_parser("prune")
    prune.add_argument("--keep", type=int, default=3)
    prune.add_argument("--apply", action="store_true")
    restore = sub.add_parser("restore")
    restore.add_argument("snapshot")
    sub.add_parser("config")
    diagnostics = sub.add_parser("diagnostics")
    diagnostics.add_argument("--live", action="store_true")
    setting = sub.add_parser("set")
    setting.add_argument("key")
    owner_approval = sub.add_parser("owner-approval")
    owner_approval.add_argument("action", choices=("list", "approve"))
    owner_approval.add_argument("token", nargs="?")
    owner_approval.add_argument("target_id", nargs="?", type=int)
    for action in ("start", "stop", "restart"):
        sub.add_parser(action)
    logs = sub.add_parser("logs")
    logs.add_argument("--lines", type=int, default=80)
    remove = sub.add_parser("remove")
    remove.add_argument("--purge", action="store_true")
    remove.add_argument("--yes", action="store_true")
    sub.add_parser("abort-install")
    args = parser.parse_args()
    manager = Manager()
    try:
        if args.command == "status":
            result: Any = manager.status()
        elif args.command == "preflight":
            result = manager.preflight()
        elif args.command == "deps":
            result = manager.install_os_dependencies()
        elif args.command == "versions":
            result = manager.versions(refresh=args.refresh)
        elif args.command == "install":
            if not args.branch:
                raise ManagerError("Specify --branch explicitly; no version is installed automatically")
            if args.code_only:
                config = None
            elif args.config:
                if args.config.stat().st_mode & 0o077:
                    raise ManagerError("Configuration file must be private (chmod 600)")
                config = _parse_env(args.config)
            else:
                if not sys.stdin.isatty():
                    raise ManagerError("Use --config FILE in noninteractive mode")
                config = {"BOT_TOKEN": getpass.getpass("Telegram bot token: "),
                          "OWNER_TELEGRAM_ID": input("Owner numeric ID: ").strip(),
                          "ALLOWED_TELEGRAM_IDS": input("Allowed numeric IDs (comma separated): ").strip(),
                          "ALLOWED_GROUP_IDS": input("Allowed group IDs (optional): ").strip(),
                          "PRIVATE_CHATS_ONLY": "true"}
                if config["ALLOWED_GROUP_IDS"]:
                    config["PRIVATE_CHATS_ONLY"] = "false"
            result = manager.install(config, branch=args.branch, commit=args.commit,
                                     checks=not args.skip_checks)
        elif args.command == "update":
            result = manager.update(checks=not args.skip_checks)
        elif args.command == "switch":
            result = manager.switch(args.branch, commit=args.commit, checks=not args.skip_checks)
        elif args.command == "rollback":
            result = manager.rollback()
        elif args.command == "recover":
            result = manager.recover()
        elif args.command == "backup":
            result = manager.backup()
        elif args.command == "backups":
            result = manager.backups()
        elif args.command == "queue":
            result = manager.queue()
        elif args.command == "storage":
            result = manager.storage()
        elif args.command == "cleanup":
            result = manager.cleanup_transient()
        elif args.command == "prune":
            result = manager.prune_releases(keep=args.keep, apply=args.apply)
        elif args.command == "restore":
            if input("Restoring discards later database changes. Type RESTORE: ") != "RESTORE":
                raise ManagerError("Cancelled")
            result = manager.restore(args.snapshot)
        elif args.command == "config":
            result = manager.config()
        elif args.command == "diagnostics":
            result = manager.diagnostics(live=args.live)
        elif args.command == "set":
            value = getpass.getpass("New value: ") if args.key in PRIVATE_KEYS else input("New value: ")
            result = manager.set_config(args.key, value)
        elif args.command == "owner-approval":
            if args.action == "list":
                result = manager.owner_approvals()
            else:
                if not args.token or args.target_id is None:
                    raise ManagerError("Use owner-approval approve TOKEN TARGET_ID")
                if input(f"Type APPROVE OWNER {args.target_id}: ") != \
                        f"APPROVE OWNER {args.target_id}":
                    raise ManagerError("Cancelled")
                result = manager.approve_owner(args.token, args.target_id)
        elif args.command in {"start", "stop", "restart"}:
            result = manager.service(args.command)
        elif args.command == "abort-install":
            if input("Type ABORT INSTALL: ") != "ABORT INSTALL":
                raise ManagerError("Cancelled")
            result = manager.abort_incomplete_install()
        elif args.command == "logs":
            result = manager.logs(args.lines)
        else:
            if not args.yes or (args.purge and input("Type PURGE MIKACHAN: ") != "PURGE MIKACHAN"):
                raise ManagerError("Use --yes to remove; --purge also requires typed confirmation")
            result = manager.remove(purge=args.purge)
        print(json.dumps(result, indent=2, ensure_ascii=False) if isinstance(result, (dict, list))
              else result)
    except (ManagerError, OSError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    _cli()
