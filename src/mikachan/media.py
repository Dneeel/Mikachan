"""Artifact checks and valid Telegram media-group partitioning."""

import asyncio
import json
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def gallery_budget(max_file_bytes: int) -> int:
    """Allow multiple Telegram-size items while bounding one gallery's disk use."""
    return max(max_file_bytes, min(max_file_bytes * 6, 300 * 1024 * 1024))


def media_kind(path: Path, streams: dict[str, bool]) -> str:
    if path.suffix.lower() in IMAGE_EXT:
        return "photo"
    if streams["video"]:
        return "video"
    if streams["audio"]:
        return "audio"
    return "document"


def groups(items: list[Path]) -> list[list[Path]]:
    """Partition preserving order, with only 2..10 grouped items; singleton is sent alone."""
    return [items[i : i + 10] for i in range(0, len(items), 10)]


async def verify(path: Path, max_bytes: int) -> dict[str, bool]:
    if not path.is_file() or not 0 < path.stat().st_size <= max_bytes:
        raise ValueError("empty or oversize artifact")
    known_media = path.suffix.lower() in {
        ".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".ogg", ".flac", ".opus", ".aac",
    }
    if path.suffix.lower() in IMAGE_EXT:
        with path.open("rb") as stream:
            header = stream.read(12)
        suffix = path.suffix.lower()
        valid = (
            header.startswith(b"\xff\xd8\xff") if suffix in {".jpg", ".jpeg"}
            else header.startswith(b"\x89PNG\r\n\x1a\n") if suffix == ".png"
            else header.startswith(b"RIFF") and header[8:12] == b"WEBP"
        )
        if not valid:
            raise ValueError("image content does not match its format")
        return {"video": False, "audio": False}
    if path.suffix.lower() in {".pdf", ".zip"}:
        return {"video": False, "audio": False}
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), 20)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ValueError("media probe timed out") from None
    if proc.returncode:
        if not known_media:
            return {"video": False, "audio": False}
        raise ValueError("media probe failed")
    details = json.loads(stdout)
    streams = details.get("streams", [])
    result = {
        "video": any(x.get("codec_type") == "video" for x in streams),
        "audio": any(x.get("codec_type") == "audio" for x in streams),
    }
    if not any(result.values()):
        raise ValueError("media has no playable streams")
    raw_duration = (details.get("format") or {}).get("duration")
    try:
        duration = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration = None
    if result["video"] and duration is not None and duration < 0.5:
        raise ValueError("video has no meaningful duration")
    return result
