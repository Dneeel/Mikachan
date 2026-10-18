# Ubuntu staging deployment

The Freestyle VM deployment uses `/opt/mikachan`, a dedicated `mikachan` account, `/var/lib/mikachan` for state, and a root-readable `/etc/mikachan.env`. The systemd unit is installed at `/etc/systemd/system/mikachan.service`. The VM can be paused after verification to preserve its free runtime quota; resume it before testing the bot.

Use `deploy/setup-ubuntu.sh --dry-run` to inspect planned actions. The script's `--install` mode requires `uv`, creates an isolated `.venv` in the checkout, and runs tests. For another Ubuntu host, create the dedicated service account and private state directory, copy `deploy/mikachan.service`, set `DATA_DIR=/var/lib/mikachan` in `/etc/mikachan.env`, then run `systemctl daemon-reload && systemctl enable --now mikachan`. Do not assume any port is free.

For the official local Bot API: obtain the free Telegram `API_ID` and `API_HASH`, install the official `telegram-bot-api` server separately, bind it to an available loopback port, and verify it. Stop public polling first; explicitly call Telegram Bot API `logOut`, then set `BOT_API_MODE=local` and `LOCAL_BOT_API_URL=http://127.0.0.1:PORT`. Run `doctor` and a small upload smoke test before enabling large uploads. Never run cloud and local polling simultaneously. Return to public mode only with the required documented migration procedure.

Backup SQLite with its SQLite backup API or after stopping the service; do not copy only the main file while WAL writes are active. To roll back, stop service, restore the previously backed-up DB/code in an isolated directory, and restart only after verifying the Bot API mode.
