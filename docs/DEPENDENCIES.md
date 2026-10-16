# Dependencies and notices

The Python project directly pins `aiogram`, `httpx`, `yt-dlp`, `gallery-dl`, `feedparser`, `ytmusicapi`, and `pydantic-settings`. `gallery-dl` is used only as a fallback for public TikTok photo posts. Development tools are `pytest`, `pytest-asyncio`, `ruff`, and `mypy`. FFmpeg/ffprobe and optional Deno are external runtime programs. `uv` manages the isolated environment. The code is MIT-licensed (see `LICENSE`); external packages and programs retain their own upstream licenses. No third-party source code was copied into this repository.

Dependencies are locked in `uv.lock` and installed on the supplied Freestyle VM. Review upstream license and advisory metadata before production distribution.
