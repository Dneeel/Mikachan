# Dependencies and notices

The Python project directly pins `aiogram`, `httpx`, `yt-dlp`, `feedparser`, `ytmusicapi`, and `pydantic-settings`. Development tools are `pytest`, `pytest-asyncio`, `ruff`, and `mypy`. FFmpeg/ffprobe and optional Deno are external runtime programs. `uv` manages the isolated environment. The code is MIT-licensed (see `LICENSE`); external packages and programs retain their own upstream licenses. No third-party source code was copied into this repository.

Package resolution and upstream license/advisory verification were not possible in this workspace because the network-enabled install was interrupted. Review installed package metadata, generate and commit `uv.lock`, run vulnerability checks, and retain applicable notices before production distribution.
