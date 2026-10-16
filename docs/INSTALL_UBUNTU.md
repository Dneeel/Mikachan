# Ubuntu staging deployment

Run only on an explicitly approved host. This repository does not touch any existing VPS services.

Use `deploy/setup-ubuntu.sh --dry-run` to inspect planned actions. The script's `--install` mode requires `uv`, creates an isolated `.venv` in the checkout, and runs tests. Create a dedicated non-root service account and private state directory separately. For the systemd unit set `DATA_DIR=/var/lib/mika-nexus` in `.env` and give only the service user access to that directory. Copy `deploy/mika-nexus.service` and replace `YOUR_USER`/`/opt/mika-nexus` after reviewing system paths and available resources. Do not assume any port is free.

For the official local Bot API: obtain the free Telegram `API_ID` and `API_HASH`, install the official `telegram-bot-api` server separately, bind it to an available loopback port, and verify it. Stop public polling first; explicitly call Telegram Bot API `logOut`, then set `BOT_API_MODE=local` and `LOCAL_BOT_API_URL=http://127.0.0.1:PORT`. Run `doctor` and a small upload smoke test before enabling large uploads. Never run cloud and local polling simultaneously. Return to public mode only with the required documented migration procedure.

Backup SQLite with its SQLite backup API or after stopping the service; do not copy only the main file while WAL writes are active. To roll back, stop service, restore the previously backed-up DB/code in an isolated directory, and restart only after verifying the Bot API mode.
