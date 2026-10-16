"""Artifact checks and valid Telegram media-group partitioning."""

import asyncio
import json
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


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
    if path.suffix.lower() in IMAGE_EXT | {".pdf", ".zip"}:
        return {"video": False, "audio": False}
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
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
    streams = json.loads(stdout).get("streams", [])
    result = {
        "video": any(x.get("codec_type") == "video" for x in streams),
        "audio": any(x.get("codec_type") == "audio" for x in streams),
    }
    if not any(result.values()):
        raise ValueError("media has no playable streams")
    return result
