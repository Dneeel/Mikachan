"""Prepare a private n8n relay credential without rotating existing secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import secrets
import stat
import urllib.error
import urllib.request
from pathlib import Path

from deploy.n8n.reconcile_ai import Client, ReconcileError, _private_file, _save


def verify_webhook(origin: str, secret: str) -> bool:
    """Check Header Auth with harmless invalid payload, never a Telegram action."""
    Client(origin, "probe")  # Validate that the origin is private loopback or HTTPS.
    if len(secret) < 40:
        return False

    def probe(value: str) -> int:
        request = urllib.request.Request(origin.rstrip("/") + "/webhook/mikachan-relay",
            data=b"{}", method="POST", headers={"X-Mikachan-Auth": value,
                 "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except (urllib.error.URLError, TimeoutError):
            return 0

    denied = probe("invalid-" + secrets.token_hex(32))
    accepted = probe(secret) if denied in {401, 403} else 0
    return denied in {401, 403} and accepted not in {0, 401, 403, 404}


def _valid_secret_file(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        owner = pwd.getpwnam("mikachan").pw_uid
    except KeyError:
        return False
    info = path.stat()
    return info.st_uid == owner and stat.S_IMODE(info.st_mode) == 0o600


def prepare(origin: str, key_file: Path, *, bot_root: Path = Path("/opt/Mikachan"),
            n8n_root: Path = Path("/opt/n8n")) -> dict[str, str]:
    """Create only a missing credential; never replace one already in n8n."""
    client = Client(origin, _private_file(key_file))
    name = "Mikachan Private Relay"
    rows = client.request("GET", "/credentials?limit=100").get("data", [])
    found = [row for row in rows if row.get("type") == "httpHeaderAuth" and
             row.get("name") == name]
    if len(found) > 1:
        return {"status": "BLOCKED_CONFLICT", "reason": "Duplicate relay credentials"}
    marker = n8n_root / "ai/relay-owned.json"
    secret_file = bot_root / "config/n8n-relay.secret"
    if found:
        if marker.is_file():
            record = json.loads(marker.read_text())
            if record.get("credential_id") == found[0]["id"] and _valid_secret_file(secret_file) and \
                    hashlib.sha256(secret_file.read_bytes().strip()).hexdigest() == record.get("hash"):
                return {"status": "WEBHOOK_CREDENTIAL_READY", "ownership": "managed"}
        if _valid_secret_file(secret_file) and \
                verify_webhook(origin, secret_file.read_text().strip()):
            return {"status": "WEBHOOK_CREDENTIAL_READY", "ownership": "verified_existing"}
        return {"status": "SETUP_REQUIRED", "reason": "Existing Mikachan Private Relay credential was preserved; "
                "verify its secret matches the bot before enabling the relay"}
    if not (bot_root / "config/bot.env").is_file():
        return {"status": "SETUP_REQUIRED", "reason": "Complete Mikachan bot Setup first"}
    try:
        bot_uid = pwd.getpwnam("mikachan").pw_uid
        bot_gid = pwd.getpwnam("mikachan").pw_gid
    except KeyError:
        return {"status": "SETUP_REQUIRED", "reason": "Mikachan service user is unavailable"}
    if secret_file.is_symlink():
        raise ReconcileError("Relay secret path must not be a symlink")
    if secret_file.exists():
        info = secret_file.stat()
        if info.st_uid != bot_uid or stat.S_IMODE(info.st_mode) != 0o600:
            raise ReconcileError("Existing relay secret permissions are unsafe")
        secret = secret_file.read_text().strip()
    else:
        secret = secrets.token_hex(32)
        secret_file.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, (secret + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chown(secret_file, bot_uid, bot_gid)
    if len(secret) < 40 or "\n" in secret:
        raise ReconcileError("Relay secret is invalid")
    created = client.request("POST", "/credentials", {"name": name,
        "type": "httpHeaderAuth", "data": {"name": "X-Mikachan-Auth", "value": secret}})
    _save(marker, {"credential_id": created["id"],
                   "hash": hashlib.sha256(secret.encode()).hexdigest()})
    return {"status": "WEBHOOK_CREDENTIAL_READY", "ownership": "managed"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1:5678")
    parser.add_argument("--key-file", type=Path, default=Path("/opt/n8n/ai/api-key"))
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        secret_file = Path("/opt/Mikachan/config/n8n-relay.secret")
        if not _valid_secret_file(secret_file):
            result = {"status": "SETUP_REQUIRED", "reason": "Relay secret is unavailable"}
        else:
            result = {"status": "WEBHOOK_READY"} if verify_webhook(
                args.origin, secret_file.read_text().strip()) else \
                {"status": "SETUP_REQUIRED", "reason": "Webhook authentication did not verify"}
        print(json.dumps(result))
        return
    print(json.dumps(prepare(args.origin, args.key_file)))


if __name__ == "__main__":
    main()
