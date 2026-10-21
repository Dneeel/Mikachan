"""Bounded curses intro playback; never executes the script as shell commands."""

from __future__ import annotations

import curses
import os
import random
import select
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from deploy.terminal_assets import DIRECTIVE, Appearance, media_candidate, read_intro

SKIP_KEYS = {ord(" "), 10, 13, curses.KEY_ENTER}
SHADES = " .:-=+*#%@"


def intro_events(script: str) -> Iterator[tuple[str, str]]:
    """Parse only known presentation directives; all other text is literal."""
    position = 0
    for match in DIRECTIVE.finditer(script):
        for char in script[position:match.start()]:
            yield "char", char
        action = match.group(1)
        yield action.split(" ", 1)[0], action.partition(" ")[2]
        position = match.end()
    for char in script[position:]:
        yield "char", char


def wait_for_key(win: Any, seconds: float, *, skippable: bool) -> bool:
    """Return True on skip without blocking longer than 50 ms per poll."""
    deadline = time.monotonic() + max(0, seconds)
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        win.timeout(max(1, min(50, int(remaining * 1000))))
        try:
            key = win.getch()
        except KeyboardInterrupt:
            return True
        if skippable and key in SKIP_KEYS:
            return True
    return False


def _line(win: Any, row: int, col: int, line: str, style: int = 0) -> None:
    height, width = win.getmaxyx()
    if 0 <= row < height and 0 <= col < width:
        try:
            win.addnstr(row, col, line, max(0, width - col - 1), style)
        except curses.error:
            pass


def black_frame(win: Any, *, pause: float = 0.2) -> None:
    win.erase()
    win.refresh()
    # The black frame is a deliberate, short visual transition, not a worker.
    if pause:
        time.sleep(pause)


def _text_frame(win: Any, text: str) -> None:
    win.erase()
    height, width = win.getmaxyx()
    lines = text.split("\n")
    visible = lines[-max(1, height - 4):]
    y = max(1, (height - len(visible)) // 2)
    x = max(2, (width - max((len(line) for line in visible), default=0)) // 2)
    for line in visible:
        _line(win, y, x, line, curses.A_BOLD)
        y += 1
    win.refresh()


def play_native(win: Any, script: str, *, skippable: bool = True) -> str:
    text = ""
    speed = 1 / 12
    for kind, value in intro_events(script):
        if kind == "speed":
            speed = 1 / int(value)
        elif kind == "slow":
            speed = 1 / 5
        elif kind == "pause":
            if wait_for_key(win, float(value), skippable=skippable):
                black_frame(win, pause=0)
                return "skipped"
        elif kind == "char":
            text += value
            _text_frame(win, text)
            if wait_for_key(win, speed if value not in "\n" else 0.04,
                            skippable=skippable):
                black_frame(win, pause=0)
                return "skipped"
        elif kind == "glitch":
            rng = random.Random(23)
            for _ in range(int(value)):
                scrambled = "".join(rng.choice("#@$%&!?/") if char.isalpha() and
                                    rng.random() < 0.4 else char for char in text)
                _text_frame(win, scrambled)
                if wait_for_key(win, 0.09, skippable=skippable):
                    black_frame(win, pause=0)
                    return "skipped"
            _text_frame(win, text)
        elif kind == "clear":
            black_frame(win)
            return "complete"
    if not script.rstrip().endswith("[clear]"):
        black_frame(win)
    return "complete"


def play_media(win: Any, path: Path, *, skippable: bool = True) -> str:
    """Stream silent grayscale frames from ffmpeg; bound time, output and CPU."""
    width, rows = win.getmaxyx()[1], win.getmaxyx()[0]
    columns = min(64, max(24, width - 8))
    lines = min(18, max(8, rows - 7))
    frame_bytes = columns * lines
    command = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
               "-vf", (f"fps=8,scale={columns}:{lines}:force_original_aspect_ratio=decrease,"
                       f"pad={columns}:{lines}:(ow-iw)/2:(oh-ih)/2,format=gray"),
               "-frames:v", "160", "-an", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    frames = 0
    partial = bytearray()
    deadline = time.monotonic() + 24
    try:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        os.set_blocking(fd, False)
        while time.monotonic() < deadline and frames < 160:
            if skippable:
                win.timeout(1)
                try:
                    if win.getch() in SKIP_KEYS:
                        black_frame(win, pause=0)
                        return "skipped"
                except KeyboardInterrupt:
                    black_frame(win, pause=0)
                    return "skipped"
            available, _, _ = select.select([fd], [], [], 0.05)
            if available:
                chunk = os.read(fd, min(65536, frame_bytes - len(partial)))
                if not chunk:
                    break
                partial.extend(chunk)
            if len(partial) == frame_bytes:
                win.erase()
                top = max(1, (win.getmaxyx()[0] - lines) // 2)
                left = max(1, (win.getmaxyx()[1] - columns) // 2)
                for row in range(lines):
                    pixels = partial[row * columns:(row + 1) * columns]
                    picture = "".join(SHADES[value * (len(SHADES) - 1) // 256]
                                      for value in pixels)
                    _line(win, top + row, left, picture)
                win.refresh()
                partial.clear()
                frames += 1
                if wait_for_key(win, 0.12, skippable=skippable):
                    black_frame(win, pause=0)
                    return "skipped"
            elif proc.poll() is not None and not available:
                break
        if frames and proc.poll() is None:
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        if frames and proc.poll() == 0:
            black_frame(win)
            return "complete"
        return "unavailable"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        if proc.stdout:
            proc.stdout.close()


def play_startup(win: Any, root: Path, setting: Appearance) -> tuple[str, str]:
    if not setting.intro_enabled:
        return "disabled", "Intro disabled"
    columns, rows = win.getmaxyx()[1], win.getmaxyx()[0]
    colors = curses.COLORS if curses.has_colors() else 0
    if setting.playback in {"auto", "media"}:
        candidate, reason = media_candidate(root, columns=columns, rows=rows, colors=colors)
        if candidate:
            try:
                result = play_media(win, candidate, skippable=setting.allow_skip)
                if result != "unavailable":
                    return result, reason
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
    script, reason = read_intro(root)
    return play_native(win, script, skippable=setting.allow_skip), reason
