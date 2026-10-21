"""Local, validated terminal appearance assets. No bot runtime imports."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

ORIGINAL_INTRO = ("[speed 12]\n"
                  "$ whoami[pause 1]?[pause 0.3]\n"
                  "[pause 1]\n"
                  "Mikachan[pause 1].[pause 0.3].[pause 0.3].[pause 1]\n"
                  "someone who thinks about you <3\n"
                  "[pause 1.5]\n"
                  "$ cat feelings.txt\n"
                  "[slow]\n"
                  "[pause 1]\n"
                  "I like you.\n"
                  "[pause 2]\n"
                  "A lot.\n"
                  "[glitch 3]\n"
                  "[clear]\n")

DEFAULT_HEADER = ("M I K A C H A N",)
COMPACT_HEADER = ("MIKACHAN",)
PREF_NAME = "appearance.json"
MAX_ART_BYTES = 16_384
MAX_INTRO_BYTES = 8_192
MAX_MEDIA_BYTES = 25 * 1024 * 1024
DIRECTIVE = re.compile(r"\[(speed [1-9][0-9]?|pause (?:[0-9]|[0-9]\.\d{1,2})|slow|glitch [1-5]|clear)\]")


def project_root(module: Path | None = None) -> Path:
    """Resolve source checkout or bootstrap panel without a fixed VPS path."""
    module = (module or Path(__file__)).resolve()
    parent = module.parent.parent
    if module.parents[2].name == "releases":
        return module.parents[3]
    return parent.parent if parent.name == "panel" else parent


def asset_root(root: Path) -> Path:
    return root / "assets" / "terminal"


def _safe_file(path: Path, limit: int) -> bytes | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_size == 0:
            return None
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        return data if len(data) <= limit else None
    except OSError:
        return None


def ensure_assets(root: Path) -> None:
    """Create directories and the original script without overwriting owner art."""
    base = asset_root(root)
    for directory in (base, base / "intro", base / "artwork"):
        if directory.is_symlink():
            raise ValueError("Terminal asset directory cannot be a symlink")
        directory.mkdir(parents=True, exist_ok=True)
    script = base / "intro" / "intro.txt"
    if not script.exists() and not script.is_symlink():
        script.write_text(ORIGINAL_INTRO, encoding="utf-8")
    manual = base / "README.md"
    source = root / "current" / "assets" / "terminal" / "README.md"
    if not manual.exists() and not manual.is_symlink() and source.is_file():
        manual.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


@dataclass(frozen=True)
class Appearance:
    intro_enabled: bool = True
    playback: str = "auto"
    allow_skip: bool = True
    header: str = "custom"
    alignment: str = "center"
    layout: str = "auto"

    def valid(self) -> bool:
        return (self.playback in {"auto", "native", "media"}
                and self.header in {"custom", "default"}
                and self.alignment in {"center", "left"}
                and self.layout in {"auto", "normal", "compact"}
                and type(self.intro_enabled) is bool and type(self.allow_skip) is bool)


def load_appearance(root: Path) -> Appearance:
    path = asset_root(root) / PREF_NAME
    data = _safe_file(path, 4096)
    if data is None:
        return Appearance()
    try:
        values = json.loads(data.decode("utf-8"))
        setting = Appearance(**values)
        return setting if setting.valid() else Appearance()
    except (ValueError, TypeError, UnicodeDecodeError):
        return Appearance()


def save_appearance(root: Path, setting: Appearance) -> None:
    if not setting.valid():
        raise ValueError("Invalid appearance settings")
    ensure_assets(root)
    base = asset_root(root)
    target = base / PREF_NAME
    if target.is_symlink():
        raise ValueError("Appearance file cannot be a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".appearance-", dir=base)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(asdict(setting), stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_header(root: Path, *, use_custom: bool = True) -> tuple[tuple[str, ...], str]:
    if not use_custom:
        return DEFAULT_HEADER, "Default header selected"
    path = asset_root(root) / "artwork" / "header.txt"
    raw = _safe_file(path, MAX_ART_BYTES)
    if raw is None:
        return DEFAULT_HEADER, "Custom header missing or invalid; default shown"
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        return DEFAULT_HEADER, "Custom header is not UTF-8; default shown"
    lines = value.splitlines()
    if (not 1 <= len(lines) <= 40 or max(map(len, lines)) > 200 or
            any(ch not in "\n\r" and (ord(ch) < 32 or ord(ch) == 127 or
                                      ord(ch) > 126) for ch in value)):
        return DEFAULT_HEADER, "Header must contain printable ASCII, at most 40 lines × 200 columns"
    return tuple(lines), "Custom header loaded"


def read_intro(root: Path) -> tuple[str, str]:
    path = asset_root(root) / "intro" / "intro.txt"
    raw = _safe_file(path, MAX_INTRO_BYTES)
    if raw is None:
        return ORIGINAL_INTRO, "Using built-in intro script"
    try:
        script = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ORIGINAL_INTRO, "Invalid UTF-8 intro; using built-in script"
    if any(ord(ch) < 32 and ch not in "\n\r" for ch in script):
        return ORIGINAL_INTRO, "Control characters in intro; using built-in script"
    remaining = DIRECTIVE.sub("", script)
    if "[" in remaining or "]" in remaining:
        return ORIGINAL_INTRO, "Unknown intro directive; using built-in script"
    return script, "Intro script loaded"


def media_candidate(root: Path, *, columns: int, rows: int,
                    colors: int) -> tuple[Path | None, str]:
    """Check format and terminal capability before invoking any media decoder."""
    if columns < 80 or rows < 24 or colors < 16:
        return None, "Media needs an 80×24 color terminal; native text will play"
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return None, "ffmpeg and ffprobe are required for terminal media; native text will play"
    directory = asset_root(root) / "intro"
    for name, signature in (("intro.mp4", b"ftyp"), ("intro.gif", b"GIF8")):
        path = directory / name
        raw = _safe_file(path, MAX_MEDIA_BYTES)
        if raw is None:
            continue
        if (name.endswith(".mp4") and raw[4:8] != signature or
                name.endswith(".gif") and not raw.startswith(signature)):
            continue
        try:
            check = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
                capture_output=True, text=True, timeout=4, check=False)
            data = json.loads(check.stdout)
            stream = data["streams"][0]
            duration = float(data["format"].get("duration", 0))
            if (check.returncode == 0 and 0 < duration <= 20 and
                    1 <= int(stream["width"]) <= 1920 and
                    1 <= int(stream["height"]) <= 1080):
                return path, f"{name} ready for silent monochrome terminal playback"
        except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.TimeoutExpired):
            pass
    return None, "No valid MP4/GIF found; native text will play"
