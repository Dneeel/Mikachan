import argparse
import asyncio
import getpass
import shutil
import subprocess
from pathlib import Path
from .config import ProviderSettings, Settings


def main() -> None:
    p = argparse.ArgumentParser(prog="mikachan")
    p.add_argument("command", choices=["run", "doctor", "import-content", "userbot-login"])
    p.add_argument("path", nargs="?")
    args = p.parse_args()
    if args.command == "userbot-login":
        from telethon import TelegramClient  # type: ignore[import-untyped]
        from telethon.sessions import StringSession  # type: ignore[import-untyped]

        async def login() -> None:
            api_id = int(input("Telegram app API ID: ").strip())
            api_hash = getpass.getpass("Telegram app API hash: ").strip()
            phone = input("Telegram user account phone: ").strip()
            client = TelegramClient(StringSession(), api_id, api_hash)
            try:
                await client.start(
                    phone=phone,
                    code_callback=lambda: getpass.getpass("Telegram login code: "),
                    password=lambda: getpass.getpass("Telegram 2FA password, if requested: "),
                )
                peer = await client.get_entity("CoinPJbot")
                if not getattr(peer, "bot", False):
                    raise ValueError("CoinPJbot did not resolve to a Telegram bot")
                print("Paste this secret only into Configuration → COINPJ_SESSION:")
                print(client.session.save())
            finally:
                await client.disconnect()

        asyncio.run(login())
        return
    if args.command == "import-content":
        if not args.path:
            p.error("import-content requires a reviewed JSON file")
        from .community import CommunityStore
        from .store import Store

        store = Store(Settings().data_dir / "mikachan.sqlite")  # type: ignore[call-arg]
        count = CommunityStore(store.db).import_content(Path(args.path))
        print(f"Imported {count} reviewed content items")
        return
    if args.command == "doctor":
        for name in ("ffmpeg", "ffprobe", "yt-dlp", "gallery-dl", "deno", "tesseract"):
            print(f"{name}: {shutil.which(name) or 'missing'}")
        if shutil.which("tesseract"):
            languages = subprocess.run(["tesseract", "--list-langs"], capture_output=True,
                                       text=True, timeout=5).stdout
            print("Persian OCR: " + ("available" if "fas" in languages else "language data missing"))
        try:
            import cairosvg  # type: ignore[import-untyped]  # noqa: F401
        except (ImportError, OSError):
            print("TGS rendering: CairoSVG/Cairo unavailable")
        else:
            print("TGS rendering: CairoSVG/Cairo available")
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
