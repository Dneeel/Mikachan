# Dependencies and notices

The Python project directly pins `aiogram`, `httpx`, `yt-dlp`, `gallery-dl`, `feedparser`, `ytmusicapi`, and `pydantic-settings`. The YouTube community-post extractor `yt-dlp-community-plugin` is pinned to Git commit `7966b80209c1c1355b51469f1a338c90bd8b91f9` (upstream Unlicense) in `uv.lock`. `gallery-dl` is used as a fallback for public image posts. Development tools are `pytest`, `pytest-asyncio`, `ruff`, and `mypy`. FFmpeg/ffprobe and Deno are external runtime programs. `uv` manages the isolated environment. The code is MIT-licensed (see `LICENSE`); external packages and programs retain their own upstream licenses. No third-party source code was copied into this repository.

Dependencies are locked in `uv.lock` and installed on the supplied Freestyle VM. Review upstream license and advisory metadata before production distribution.
