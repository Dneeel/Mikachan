import argparse
import asyncio
import shutil
from .config import ProviderSettings, Settings


def main() -> None:
    p = argparse.ArgumentParser(prog="mikachan")
    p.add_argument("command", choices=["run", "doctor"])
    args = p.parse_args()
    if args.command == "doctor":
        for name in ("ffmpeg", "ffprobe", "yt-dlp", "gallery-dl", "deno"):
            print(f"{name}: {shutil.which(name) or 'missing'}")
        try:
            s = Settings()  # type: ignore[call-arg]  # values are loaded from the environment
            print(f"config: valid; mode={s.bot_api_mode}; users={len(s.allowlist)}")
        except Exception:
            print("config: invalid; check required values in .env.example")
        providers = ProviderSettings()
        print("Spotify catalog: " + (
            "configured" if providers.spotify_client_id and providers.spotify_client_secret
            else "credentials not configured"
        ))
        print("provider status: unverified until live acceptance")
        return
    from .bot import App

    asyncio.run(App(Settings()).run())  # type: ignore[call-arg]
