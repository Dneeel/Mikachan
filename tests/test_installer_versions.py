"""Version selection is explicit; staging never silently switches to v2."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from deploy.manager import BRANCH
from deploy.manager_ui import Entry, MikachanMenu

ROOT = Path(__file__).resolve().parents[1]


def test_pytest_console_entrypoint_can_collect_manager_tests() -> None:
    """The release manager invokes .venv/bin/pytest, not python -m pytest."""
    pytest_command = Path(sys.executable).with_name("pytest")
    run = subprocess.run(
        [str(pytest_command), "--collect-only", "-q", "tests/test_installer_versions.py"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.fixture
def fake_git(tmp_path: Path) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "git"
    fake.write_text("""#!/bin/sh
if [ \"$1\" = ls-remote ]; then
  printf '%s\\n' \\
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa refs/heads/v2-test' \\
    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb refs/heads/v1' \\
    'cccccccccccccccccccccccccccccccccccccccc refs/heads/v2' \\
    'ffffffffffffffffffffffffffffffffffffffff refs/heads/v2.1' \\
    'dddddddddddddddddddddddddddddddddddddddd refs/heads/v2-malicious' \\
    'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee refs/heads/main'
elif [ \"$1\" = check-ref-format ]; then
  exec /usr/bin/git \"$@\"
else
  exit 99
fi
""")
    fake.chmod(0o755)
    return {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"]}


def test_version_listing_is_remote_driven_and_read_only(fake_git: dict[str, str]) -> None:
    run = subprocess.run(["bash", str(ROOT / "deploy/bootstrap.sh"), "--list-versions"],
                         env=fake_git, capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["main", "v1", "v2", "v2-malicious", "v2-test", "v2.1"]


@pytest.mark.parametrize("args", [
    ["--branch", "../invalid"],
    ["--branch", "-invalid"],
    ["--branch", "v2", "--commit", "short"],
    ["--commit", "a" * 40],
])
def test_bad_options_abort_before_install(args: list[str]) -> None:
    run = subprocess.run(["bash", str(ROOT / "deploy/bootstrap.sh"), *args],
                         capture_output=True, text=True, check=False)
    assert run.returncode == 2
    assert "apt-get" not in run.stdout


def test_manager_accepts_only_explicit_supported_branches() -> None:
    assert BRANCH.fullmatch("v2-test")
    assert BRANCH.fullmatch("v2")
    assert not BRANCH.fullmatch("v2-hijack")
    assert not BRANCH.fullmatch("main")


def test_cli_does_not_default_to_v2() -> None:
    run = subprocess.run([sys.executable, str(ROOT / "deploy/manager.py"),
                          "install", "--code-only"], capture_output=True, text=True)
    assert run.returncode != 0
    assert "--branch" in run.stderr


def test_test_wrapper_forces_test_branch_last() -> None:
    script = (ROOT / "deploy/bootstrap-test.sh").read_text()
    assert 'bash "$installer" "$@" --branch v2-test' in script
    assert "raw.githubusercontent.com/Dneeel/Mikachan/v2-test/deploy/bootstrap.sh" in script


class _MenuManager:
    def status(self):
        return {"installed": False}


def test_tui_install_menu_chooses_version_instead_of_defaulting() -> None:
    menu = MikachanMenu(manager_factory=_MenuManager)
    menu._choose_branch({"branch": "v2-test", "commit": "a" * 40})
    entries = menu.page.entries
    assert entries[0] == Entry("Install v2-test@aaaaaaaaaaaa", "confirm:install", "v2-test")
    assert "v2-test" in menu.page.title
