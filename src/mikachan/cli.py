import argparse
import asyncio
import getpass
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TextIO
from .config import ProviderSettings, Settings


async def _login_with_qr(client: object, output: TextIO) -> None:
    """Authorize a real Telegram account with a short-lived terminal QR code."""
    import qrcode
    from telethon.errors import SessionPasswordNeededError  # type: ignore[import-untyped]

    await client.connect()  # type: ignore[attr-defined]
    qr_login = await client.qr_login()  # type: ignore[attr-defined]
    for attempt in range(5):
        qr = qrcode.QRCode(border=4, box_size=1)
        qr.add_data(qr_login.url)
        qr.make(fit=True)
        print("Telegram → Settings → Devices → Link Desktop Device", file=output)
        print("Scan this QR code with the Telegram app on your phone:", file=output)
        qr.print_ascii(out=output, tty=output.isatty())
        try:
            await qr_login.wait()
        except asyncio.TimeoutError:
            if attempt == 4:
                raise TimeoutError("QR login expired after five attempts; run again") from None
            print("QR expired; generating a fresh code…", file=output)
            await qr_login.recreate()
        except SessionPasswordNeededError:
            await client.sign_in(password=getpass.getpass("Telegram 2FA password: "))  # type: ignore[attr-defined]
            return
        else:
            return


def main() -> None:
    p = argparse.ArgumentParser(prog="mikachan")
    p.add_argument("command", choices=["run", "doctor", "import-content", "userbot-login"])
    p.add_argument("path", nargs="?")
    args = p.parse_args()
    if args.command == "userbot-login":
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            p.error("userbot-login requires a private interactive terminal")
        from telethon import TelegramClient  # type: ignore[import-untyped]
        from telethon.sessions import StringSession  # type: ignore[import-untyped]

        async def login() -> None:
            api_id = int(input("Telegram app API ID: ").strip())
            api_hash = getpass.getpass("Telegram app API hash: ").strip()
            client = TelegramClient(StringSession(), api_id, api_hash)
            try:
                await _login_with_qr(client, sys.stdout)
                me = await client.get_me()
                if not me or getattr(me, "bot", False):
                    raise ValueError("QR login must authorize a Telegram user account")
                print("Paste this secret only into protected USERBOT_SESSION configuration:")
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
