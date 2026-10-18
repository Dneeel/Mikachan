"""Release manager tests use disposable Git repositories and simulated systemd."""

import json
import fcntl
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy.manager import Manager, ManagerError, Paths, _parse_env, _render_env


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def commit(source: Path, message: str) -> str:
    (source / "payload.txt").write_text(message, encoding="utf-8")
    git("add", ".", cwd=source)
    git("commit", "-m", message, cwd=source)
    return git("rev-parse", "HEAD", cwd=source)


class FakeManager(Manager):
    def __init__(self, paths: Paths, source: Path):
        super().__init__(paths, repo_url=str(source), require_root=False, uv="unused")
        self.active = False
        self.fail_next_start = False

    def _create_user(self) -> None:
        pass

    def _stage(self, sha: str, *, checks: bool = True) -> Path:
        release = self.paths.releases / sha
        release.parent.mkdir(parents=True, exist_ok=True)
        if not release.exists():
            self._run("git", "--git-dir", str(self.paths.repo), "worktree", "add", "--detach",
                      str(release), sha)
        return release

    def _validate_config(self, values: dict[str, str], release: Path) -> None:
        assert values["DATA_DIR"] == str(self.paths.state)
        assert release.is_dir()

    def _systemctl(self, action: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        if action == "start":
            if self.fail_next_start:
                self.fail_next_start = False
                self.active = False
            else:
                self.active = True
        elif action == "stop":
            self.active = False
        elif action == "is-active":
            return subprocess.CompletedProcess(["systemctl"], 0 if self.active else 3,
                                               "active\n" if self.active else "inactive\n", "")
        return subprocess.CompletedProcess(["systemctl"], 0, "", "")

    def _run(self, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("systemctl", "daemon-reload"):
            return subprocess.CompletedProcess(args, 0, "", "")
        return super()._run(*args, **kwargs)  # type: ignore[arg-type]

    def _healthy(self) -> bool:
        return self.active


@pytest.fixture
def deployment(tmp_path: Path) -> tuple[FakeManager, Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    git("init", "-b", "v2", cwd=source)
    git("config", "user.name", "Test", cwd=source)
    git("config", "user.email", "test@example.org", cwd=source)
    (source / "uv.lock").write_text("fixture", encoding="utf-8")
    first = commit(source, "first")
    git("branch", "v1", cwd=source)
    paths = Paths(base=tmp_path / "opt" / "mikachan",
                  state=tmp_path / "var" / "lib" / "mikachan",
                  backups=tmp_path / "var" / "backups" / "mikachan",
                  env=tmp_path / "etc" / "mikachan.env",
                  unit=tmp_path / "etc" / "systemd" / "mikachan.service")
    return FakeManager(paths, source), source, first


def install(manager: FakeManager) -> None:
    manager.install({"BOT_TOKEN": "12345:" + "x" * 30,
                     "OWNER_TELEGRAM_ID": "42", "ALLOWED_TELEGRAM_IDS": "42",
                     "PRIVATE_CHATS_ONLY": "true"})


def database(manager: FakeManager) -> Path:
    path = manager.paths.state / "mikachan.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE access (user_id INTEGER PRIMARY KEY);
        CREATE TABLE allowed_groups (chat_id INTEGER PRIMARY KEY);
        CREATE TABLE group_access_seeded (singleton INTEGER PRIMARY KEY);
        CREATE TABLE jobs (id TEXT PRIMARY KEY, user_id INTEGER, chat_id INTEGER,
                           state TEXT, updated REAL);
        CREATE TABLE notes (value TEXT);
        INSERT INTO access VALUES (42);
        INSERT INTO notes VALUES ('original');
        """)
    return path


def note(path: Path) -> str:
    with sqlite3.connect(path) as db:
        return str(db.execute("SELECT value FROM notes").fetchone()[0])


def test_install_and_same_branch_update_then_database_rollback(
        deployment: tuple[FakeManager, Path, str]) -> None:
    manager, source, first = deployment
    assert {item["branch"] for item in manager.versions(refresh=True)} == {"v1", "v2"}
    install(manager)
    assert manager.status()["commit"] == first
    assert manager.status()["service"] == "active"
    assert manager.paths.current.resolve() == manager.paths.releases / first
    assert os.stat(manager.paths.env).st_mode & 0o777 == 0o640
    assert "12345:" not in manager.paths.manifest.read_text()
    assert "Already" in manager.update()
    path = database(manager)
    second = commit(source, "second on v2")
    assert "Activated v2" in manager.update()
    assert manager.status()["commit"] == second
    with sqlite3.connect(path) as db:
        db.execute("UPDATE notes SET value='newer'")
    assert "discarded" in manager.rollback()
    assert manager.status()["commit"] == first
    assert note(path) == "original"
    assert manager.status()["consistent"]
    assert manager.paths.journal.exists() is False


def test_major_switch_and_update_even_on_v4(deployment: tuple[FakeManager, Path, str]) -> None:
    manager, source, first = deployment
    install(manager)
    database(manager)
    git("checkout", "-b", "v4", cwd=source)
    v4_first = commit(source, "v4 first")
    assert "Activated v4" in manager.switch("v4")
    assert manager.status()["commit"] == v4_first
    v4_patch = commit(source, "v4 patch")
    assert "Activated v4" in manager.update()
    assert manager.status()["commit"] == v4_patch
    assert "Activated v2" in manager.switch("v2", commit=first)
    assert manager.status()["branch"] == "v2"
    assert manager.status()["commit"] == first
    with pytest.raises(ManagerError, match="not reachable"):
        manager.switch("v2", commit=v4_patch)


def test_remote_rewrite_cannot_silently_replace_installed_branch(
        deployment: tuple[FakeManager, Path, str]) -> None:
    manager, source, first = deployment
    install(manager)
    database(manager)
    commit(source, "second")
    manager.update()
    installed = manager.status()["commit"]
    git("reset", "--hard", first, cwd=source)
    with pytest.raises(ManagerError, match="diverged"):
        manager.update()
    assert manager.status()["commit"] == installed


def test_failed_activation_restores_previous_code_and_database(
        deployment: tuple[FakeManager, Path, str]) -> None:
    manager, source, first = deployment
    install(manager)
    path = database(manager)
    commit(source, "next")
    manager.fail_next_start = True
    with pytest.raises(ManagerError, match="did not stay active"):
        manager.update()
    assert manager.status()["commit"] == first
    assert manager.status()["service"] == "active"
    assert note(path) == "original"
    assert not manager.paths.journal.exists()


def test_snapshot_restore_and_authorization_sync(deployment: tuple[FakeManager, Path, str]) -> None:
    manager, _, _ = deployment
    install(manager)
    path = database(manager)
    snap = manager.backup()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE notes SET value='later'")
        db.execute("INSERT INTO jobs VALUES ('a', 77, -10, 'QUEUED', 0)")
        db.execute("INSERT INTO access VALUES (77)")
        db.execute("INSERT INTO allowed_groups VALUES (-10)")
    manager.set_config("ALLOWED_GROUP_IDS", "-20")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM access WHERE user_id=77").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM allowed_groups WHERE chat_id=-20").fetchone()[0] == 1
        assert db.execute("SELECT state FROM jobs WHERE id='a'").fetchone()[0] == "CANCELLED"
    manager.restore(snap)
    assert note(path) == "original"
    assert manager.backups()


def test_remove_preserves_state_by_default_and_purge_deletes_only_managed_paths(
        deployment: tuple[FakeManager, Path, str]) -> None:
    manager, _, _ = deployment
    install(manager)
    database(manager)
    outside = manager.paths.base.parent / "another-project"
    outside.write_text("untouched")
    assert "retained" in manager.remove()
    assert not manager.paths.base.exists()
    assert manager.paths.state.exists()
    assert manager.paths.env.exists()
    assert outside.read_text() == "untouched"
    install(manager)
    assert "backups" in manager.remove(purge=True)
    assert not manager.paths.state.exists()
    assert not manager.paths.env.exists()
    assert outside.read_text() == "untouched"


def test_unmanaged_paths_and_bad_refs_are_rejected(deployment: tuple[FakeManager, Path, str]) -> None:
    manager, _, _ = deployment
    manager.paths.base.mkdir(parents=True)
    (manager.paths.base / "legacy").write_text("do not touch")
    with pytest.raises(ManagerError, match="unmanaged"):
        install(manager)
    assert (manager.paths.base / "legacy").exists()
    (manager.paths.base / "legacy").unlink()
    install(manager)
    database(manager)
    with pytest.raises(ManagerError, match="existing major"):
        manager.switch("main")
    with pytest.raises(ManagerError, match="full 40-character"):
        manager.switch("v2", commit="abc")


def test_environment_round_trip_keeps_secrets_off_release_metadata(
        deployment: tuple[FakeManager, Path, str]) -> None:
    manager, _, _ = deployment
    values = {"BOT_TOKEN": "12345:" + "x" * 30, "NASA_API_KEY": "a#b $c 'd'",
              "ALLOWED_TELEGRAM_IDS": "42"}
    path = manager.paths.env
    path.parent.mkdir(parents=True)
    path.write_text(_render_env(values), encoding="utf-8")
    assert _parse_env(path) == values
    assert "a#b $c" in path.read_text()
    with pytest.raises(ManagerError, match="Control character"):
        _render_env({"BOT_TOKEN": "secret\nSHADOW=1"})


def test_recover_interrupted_activation(deployment: tuple[FakeManager, Path, str]) -> None:
    manager, source, first = deployment
    install(manager)
    path = database(manager)
    second = commit(source, "second")
    manager._fetch()
    manager._stage(second)
    snapshot = manager.backup()
    old = json.loads(manager.paths.manifest.read_text())["history"][-1]
    manager._switch_link(second)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE notes SET value='interrupted'")
    manager.paths.journal.write_text(json.dumps({"old": old, "target": {"branch": "v2",
        "sha": second}, "snapshot": snapshot}))
    with pytest.raises(ManagerError, match="Interrupted"):
        manager.update()
    assert "Restored interrupted" in manager.recover()
    assert manager.status()["commit"] == first
    assert note(path) == "original"


def test_update_preserves_intentionally_stopped_service(
        deployment: tuple[FakeManager, Path, str]) -> None:
    manager, source, _ = deployment
    install(manager)
    database(manager)
    manager.service("stop")
    latest = commit(source, "staged while stopped")
    manager.update()
    assert manager.status()["commit"] == latest
    assert manager.status()["service"] == "inactive"
    manager.set_config("NASA_API_KEY", "DEMO_KEY")
    assert manager.status()["service"] == "inactive"


def test_prune_keeps_current_and_previous_release(
        deployment: tuple[FakeManager, Path, str]) -> None:
    manager, source, first = deployment
    install(manager)
    database(manager)
    git("checkout", "-b", "v4", cwd=source)
    a = commit(source, "v4 a")
    b = commit(source, "v4 b")
    c = commit(source, "v4 c")
    manager.switch("v4", commit=a)
    manager.switch("v4", commit=b)
    manager.update()
    assert manager.prune_releases() == [first]
    assert (manager.paths.releases / first).exists()
    assert manager.prune_releases(apply=True) == [first]
    assert not (manager.paths.releases / first).exists()
    assert (manager.paths.releases / c).exists()
    manager.rollback()
    assert manager.status()["commit"] == b


def test_failed_initial_install_can_be_cleared_without_deleting_state(
        deployment: tuple[FakeManager, Path, str]) -> None:
    manager, _, _ = deployment
    manager.paths.state.mkdir(parents=True)
    (manager.paths.state / "keep.txt").write_text("preserve")
    with pytest.raises(ManagerError, match="unavailable"):
        manager.install({"BOT_TOKEN": "12345:" + "x" * 30,
                         "OWNER_TELEGRAM_ID": "42", "ALLOWED_TELEGRAM_IDS": "42"},
                        branch="v9")
    assert manager.status()["incomplete_install"] is True
    assert "Cleared incomplete" in manager.abort_incomplete_install()
    assert not manager.paths.base.exists()
    assert (manager.paths.state / "keep.txt").read_text() == "preserve"


def test_wal_snapshot_includes_uncheckpointed_committed_rows(
        deployment: tuple[FakeManager, Path, str]) -> None:
    manager, _, _ = deployment
    install(manager)
    path = database(manager)
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("UPDATE notes SET value='in-wal'")
        db.commit()
        snapshot = manager.backup()
        assert note(manager.paths.backups / snapshot / "mikachan.sqlite") == "in-wal"
    finally:
        db.close()


def test_active_foreign_poller_blocks_release_switch(
        deployment: tuple[FakeManager, Path, str]) -> None:
    manager, source, first = deployment
    install(manager)
    database(manager)
    commit(source, "new")
    with (manager.paths.state / "polling.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ManagerError, match="Another local bot poller"):
            manager.update()
    assert manager.status()["commit"] == first
    assert manager.status()["service"] == "active"


def test_manager_validates_against_real_runtime_settings(tmp_path: Path) -> None:
    paths = Paths(state=tmp_path / "persistent")
    manager = Manager(paths, require_root=False)
    release = Path(__file__).resolve().parents[1]
    values = {"BOT_TOKEN": "12345:" + "x" * 30, "OWNER_TELEGRAM_ID": "42",
              "ALLOWED_TELEGRAM_IDS": "42", "DATA_DIR": str(paths.state)}
    manager._validate_config(values, release)
    with pytest.raises(ManagerError, match="Unsupported environment keys"):
        manager._validate_config({**values, "PYTHONPATH": "/tmp/other"}, release)
    with pytest.raises(ManagerError, match="Owner and allowlist"):
        manager._validate_config({**values, "OWNER_TELEGRAM_ID": "somebody"}, release)


def test_code_install_then_setup_and_owner_change(
        deployment: tuple[FakeManager, Path, str]) -> None:
    manager, _, _ = deployment
    manager.install()
    assert manager.status()["setup_required"]
    assert manager.status()["service"] == "inactive"
    with pytest.raises(ManagerError, match="Run Setup"):
        manager.service("start")
    token = "12345:" + "x" * 30
    manager.setup({"BOT_TOKEN": token, "OWNER_TELEGRAM_ID": "42",
                   "ALLOWED_TELEGRAM_IDS": "42", "ALLOWED_GROUP_IDS": ""})
    assert manager.status()["service"] == "active"
    assert manager.config()["BOT_TOKEN"] == "(set)"
    database(manager)
    manager.set_config("OWNER_TELEGRAM_ID", "77")
    with sqlite3.connect(manager.paths.state / "mikachan.sqlite") as db:
        assert {row[0] for row in db.execute("SELECT user_id FROM access")} == {77}
    assert manager.config()["OWNER_TELEGRAM_ID"] == "77"
    assert len(manager.backups()) >= 2


def test_setup_again_retains_database_and_updates_allowlist(
        deployment: tuple[FakeManager, Path, str]) -> None:
    manager, _, _ = deployment
    install(manager)
    path = database(manager)
    manager.set_config("MOMMY_TELEGRAM_IDS", "42")
    manager.setup({"BOT_TOKEN": "99999:" + "y" * 30,
                   "OWNER_TELEGRAM_ID": "88", "ALLOWED_TELEGRAM_IDS": "88",
                   "ALLOWED_GROUP_IDS": ""})
    assert note(path) == "original"
    with sqlite3.connect(path) as db:
        assert {row[0] for row in db.execute("SELECT user_id FROM access")} == {88}
    assert manager.config()["OWNER_TELEGRAM_ID"] == "88"
    assert manager.config()["BOT_TOKEN"] == "(set)"
    assert manager.config()["MOMMY_TELEGRAM_IDS"] == ""


def test_failed_repeat_setup_restores_config_database_and_service(
        deployment: tuple[FakeManager, Path, str]) -> None:
    manager, _, _ = deployment
    install(manager)
    path = database(manager)
    prior = manager.paths.env.read_text()
    manager.fail_next_start = True
    with pytest.raises(ManagerError, match="did not stay active"):
        manager.setup({"BOT_TOKEN": "99999:" + "y" * 30,
                       "OWNER_TELEGRAM_ID": "88", "ALLOWED_TELEGRAM_IDS": "88",
                       "ALLOWED_GROUP_IDS": ""})
    assert manager.paths.env.read_text() == prior
    assert note(path) == "original"
    assert manager.status()["service"] == "active"
    with sqlite3.connect(path) as db:
        assert {row[0] for row in db.execute("SELECT user_id FROM access")} == {42}
