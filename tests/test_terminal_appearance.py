"""Cinematic terminal assets remain local, bounded, and independent of services."""

from __future__ import annotations

import curses
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy.manager_ui import MikachanMenu
from deploy.terminal_assets import (ORIGINAL_INTRO, Appearance, asset_root, ensure_assets,
                                    load_appearance, media_candidate, read_header, read_intro,
                                    save_appearance, project_root)
from deploy.terminal_intro import intro_events, play_native, play_startup


class Window:
    def __init__(self, width=80, height=24):
        self.width, self.height = width, height
        self.drawn = []
        self.black_frames = 0

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.drawn.clear()
        self.black_frames += 1

    def refresh(self):
        pass

    def timeout(self, _milliseconds):
        pass

    def getch(self):
        return -1

    def addnstr(self, row, col, data, count, _style=0):
        self.drawn.append((row, col, data[:count]))


def test_original_script_exactly_matches_shipped_asset():
    project = Path(__file__).resolve().parents[1]
    assert (asset_root(project) / "intro/intro.txt").read_text() == ORIGINAL_INTRO
    assert "$ whoami" in ORIGINAL_INTRO and "$ cat feelings.txt" in ORIGINAL_INTRO
    events = list(intro_events(ORIGINAL_INTRO))
    assert ("speed", "12") in events
    assert ("glitch", "3") in events
    assert events[-2:] == [("clear", ""), ("char", "\n")]
    assert "".join(value for kind, value in events if kind == "char").count("$ ") == 2


def test_asset_creation_does_not_replace_custom_files(tmp_path):
    base = asset_root(tmp_path)
    (base / "intro").mkdir(parents=True)
    (base / "intro/intro.txt").write_text("Custom intro\n")
    ensure_assets(tmp_path)
    assert (base / "intro/intro.txt").read_text() == "Custom intro\n"
    assert (base / "artwork").is_dir()
    assert read_intro(tmp_path)[0] == "Custom intro\n"


def test_first_upgrade_loads_helpers_from_release_when_old_panel_skipped_them(tmp_path):
    project = Path(__file__).resolve().parents[1]
    root = tmp_path / "Mikachan"
    panel = root / "panel/deploy"
    release = root / "releases/abc123"
    helpers = release / "deploy"
    panel.mkdir(parents=True)
    helpers.mkdir(parents=True)
    for name in ("__init__.py", "manager.py", "manager_ui.py"):
        shutil.copyfile(project / "deploy" / name, panel / name)
    for name in ("terminal_assets.py", "terminal_intro.py"):
        shutil.copyfile(project / "deploy" / name, helpers / name)
    (root / "current").symlink_to(release)
    command = ("import deploy.manager_ui; from deploy.terminal_assets import project_root; "
               "print(project_root())")
    result = subprocess.run([sys.executable, "-c", command],
                            env={"PYTHONPATH": str(root / "panel")},
                            cwd=tmp_path,
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == str(root)
    assert project_root(helpers / "terminal_assets.py") == root


def test_header_preserves_whitespace_and_rejects_malformed_assets(tmp_path):
    ensure_assets(tmp_path)
    header = asset_root(tmp_path) / "artwork/header.txt"
    header.write_text("  /\\    \n /__\\   \n", encoding="utf-8")
    assert read_header(tmp_path)[0] == ("  /\\    ", " /__\\   ")
    header.write_text("\x1b[31mBAD", encoding="utf-8")
    assert read_header(tmp_path)[0] == ("M I K A C H A N",)
    header.write_text("X" * 17_000)
    assert read_header(tmp_path)[0] == ("M I K A C H A N",)
    header.unlink()
    header.symlink_to(tmp_path / "elsewhere")
    assert read_header(tmp_path)[0] == ("M I K A C H A N",)


def test_appearance_round_trip_and_invalid_setting_fallback(tmp_path):
    ensure_assets(tmp_path)
    value = Appearance(intro_enabled=False, playback="media", allow_skip=False,
                       header="default", alignment="left", layout="compact")
    save_appearance(tmp_path, value)
    assert load_appearance(tmp_path) == value
    path = asset_root(tmp_path) / "appearance.json"
    assert path.stat().st_mode & 0o077 == 0
    path.write_text(json.dumps({"playback": "shell"}))
    assert load_appearance(tmp_path) == Appearance()


def test_native_intro_can_skip_immediately_and_finishes_on_black_frame(monkeypatch):
    import deploy.terminal_intro as intro

    win = Window()
    waits = []
    monkeypatch.setattr(intro, "wait_for_key", lambda _win, seconds, **_kw: waits.append(seconds) or False)
    monkeypatch.setattr(intro.time, "sleep", lambda seconds: waits.append(seconds))
    assert play_native(win, ORIGINAL_INTRO) == "complete"
    assert waits[-1] == pytest.approx(0.2)
    assert win.drawn == []  # The Glitch-to-black transition cleared everything.
    monkeypatch.setattr(intro, "wait_for_key", lambda *_args, **_kw: True)
    win = Window()
    assert play_native(win, ORIGINAL_INTRO) == "skipped"
    assert win.drawn == []


def test_disabled_intro_and_media_fallback(monkeypatch, tmp_path):
    import deploy.terminal_intro as intro

    win = Window()
    assert play_startup(win, tmp_path, Appearance(intro_enabled=False)) == (
        "disabled", "Intro disabled")
    monkeypatch.setattr(intro.curses, "has_colors", lambda: True)
    monkeypatch.setattr(intro.curses, "COLORS", 256, raising=False)
    monkeypatch.setattr(intro, "media_candidate",
                        lambda *_args, **_kwargs: (None, "Media unsupported"))
    monkeypatch.setattr(intro, "play_native", lambda *_args, **_kwargs: "complete")
    assert play_startup(win, tmp_path, Appearance(playback="media")) == (
        "complete", "Using built-in intro script")


def test_media_detection_rejects_bad_files_and_handles_supported_gif(tmp_path, monkeypatch):
    import deploy.terminal_assets as assets

    ensure_assets(tmp_path)
    directory = asset_root(tmp_path) / "intro"
    gif = directory / "intro.gif"
    gif.write_bytes(b"not a GIF")
    monkeypatch.setattr(assets.shutil, "which", lambda _name: "/usr/bin/tool")
    assert media_candidate(tmp_path, columns=80, rows=24, colors=256)[0] is None
    gif.write_bytes(b"GIF89a" + b"0" * 32)
    monkeypatch.setattr(assets.subprocess, "run", lambda *_args, **_kw: SimpleNamespace(
        stdout=json.dumps({"streams": [{"width": 200, "height": 100}],
                           "format": {"duration": "3.0"}}), returncode=0))
    assert media_candidate(tmp_path, columns=80, rows=24, colors=256)[0] == gif
    assert media_candidate(tmp_path, columns=79, rows=24, colors=256)[0] is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="media tools unavailable")
def test_real_gif_playback_finishes_and_clears_frame(tmp_path, monkeypatch):
    import deploy.terminal_intro as intro

    target = tmp_path / "intro.gif"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=160x90:rate=8", "-t", "0.5", str(target)], check=True)
    monkeypatch.setattr(intro, "wait_for_key", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(intro.time, "sleep", lambda _seconds: None)
    win = Window()
    assert intro.play_media(win, target) == "complete"
    assert win.drawn == []


def test_appearance_navigation_persists_without_service_calls(tmp_path):
    class NoServiceManager:
        def __init__(self, **_kwargs):
            pass

        def service(self, _action):
            raise AssertionError("Appearance must not restart the bot")

    ui = MikachanMenu(NoServiceManager, root=tmp_path)
    assert len(ui.page.entries) == 10
    ui.page.selected = 7
    ui.handle_key(10)
    assert ui.page.title == "APPEARANCE"
    ui.handle_key(10)
    assert load_appearance(tmp_path).intro_enabled is False
    assert ui.page.selected == 0
    ui.page.selected = 5
    ui.handle_key(10)
    assert load_appearance(tmp_path).header == "default"
    ui.handle_key(27)
    assert ui.page.title == "MAIN MENU"
    assert ui.page.selected == 7


def test_appearance_menu_at_80_by_24_is_keyboard_accessible(tmp_path):
    ui = MikachanMenu(root=tmp_path)
    win = Window()
    ui.draw(win)
    assert any("MIKACHAN / SYSTEM CONTROL" in text for _, _, text in win.drawn)
    assert any("10   Exit" in text for _, _, text in win.drawn)
    ui.handle_key(curses.KEY_DOWN)
    ui.draw(win)
    assert any("02   Services" in text for _, _, text in win.drawn)
    win.width, win.height = 36, 12
    ui.draw(win)
    assert any("MIKACHAN" in text for _, _, text in win.drawn)
    assert any("Services" in text for _, _, text in win.drawn)
    ui.handle_key(27)
    ui.handle_key(ord("q"))
    assert ui.running is False
