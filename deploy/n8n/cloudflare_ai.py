"""Provision optional Cloudflare Workers AI without changing active agents.

Cloudflare account and model discovery are read-only. A bearer token is sent
directly to n8n's local Public API credential store and is never saved in a
repository or deployment environment file. Provider selection is explicit.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from deploy.n8n.reconcile_ai import Client, ReconcileError, _private_file, _save


MODEL = "@cf/google/gemma-4-26b-a4b-it"
ACCOUNT = re.compile(r"[a-f0-9]{32}\Z")


def _cloudflare(path: str, token: str) -> dict:
    request = urllib.request.Request("https://api.cloudflare.com/client/v4" + path,
        headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            body = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise ReconcileError("Cloudflare API verification failed") from exc
    if not isinstance(body, dict) or not body.get("success"):
        raise ReconcileError("Cloudflare API rejected the credential or account")
    return body


def prepare(origin: str, key_file: Path, token: str, account_id: str,
            state_dir: Path) -> dict[str, str]:
    if account_id and not ACCOUNT.fullmatch(account_id) or not 32 <= len(token) <= 4096 or \
            any(char.isspace() for char in token):
        raise ReconcileError("Cloudflare account ID or token is invalid")
    verified = _cloudflare("/user/tokens/verify", token)
    if verified.get("result", {}).get("status") != "active":
        raise ReconcileError("Cloudflare token is not active")
    if not account_id:
        accounts = _cloudflare("/accounts?per_page=50", token).get("result", [])
        if not isinstance(accounts, list) or len(accounts) != 1 or \
                not ACCOUNT.fullmatch(str(accounts[0].get("id", ""))):
            raise ReconcileError("Cloudflare account is ambiguous; enter its 32-character ID")
        account_id = str(accounts[0]["id"])
    path = "/accounts/" + account_id + "/ai/models/search?" + \
        urllib.parse.urlencode({"search": "gemma-4-26b-a4b-it"})
    result = _cloudflare(path, token).get("result", [])
    if not isinstance(result, list) or not any(item.get("name") == MODEL and
            (item.get("task") or {}).get("name") == "Text Generation"
            for item in result if isinstance(item, dict)):
        raise ReconcileError("Requested Cloudflare Workers AI model is unavailable")
    client = Client(origin, _private_file(key_file))
    rows = client.request("GET", "/credentials?limit=100").get("data", [])
    matches = [row for row in rows if row.get("type") == "openAiApi" and
               row.get("name") == "Cloudflare Workers AI"]
    if len(matches) > 1:
        return {"status": "BLOCKED_CONFLICT", "reason": "Duplicate Cloudflare AI credentials"}
    if matches:
        return {"status": "CREDENTIAL_EXISTS", "reason": "Existing n8n credential was preserved; "
                "its encrypted token and account binding cannot be verified through the Public API"}
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
    created = client.request("POST", "/credentials", {"name": "Cloudflare Workers AI",
        "type": "openAiApi", "data": {"apiKey": token, "url": url}})
    if not created.get("id"):
        raise ReconcileError("n8n did not confirm Cloudflare credential creation")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _save(state_dir / "cloudflare-ai.json", {"credential_id": str(created["id"]),
        "account_id": account_id, "model": MODEL})
    return {"status": "CREDENTIAL_READY", "model": MODEL,
            "reason": "Account model listing verified. Free quota, tool calling and inference "
                      "are unverified; both agents remain on their current models."}


def select(origin: str, key_file: Path, state_dir: Path,
           agent: str, provider: str) -> dict[str, str]:
    if agent not in {"mika", "serious"} or provider not in {"groq", "cloudflare"}:
        raise ReconcileError("Choose Mika or Serious and Groq or Cloudflare")
    client = Client(origin, _private_file(key_file))
    if provider == "cloudflare":
        record = state_dir / "cloudflare-ai.json"
        if record.is_symlink():
            raise ReconcileError("Cloudflare credential marker is unsafe")
        expected = json.loads(record.read_text()).get("credential_id") if record.is_file() else None
        rows = client.request("GET", "/credentials?limit=100").get("data", [])
        matches = [row for row in rows if row.get("type") == "openAiApi" and
                   row.get("name") == "Cloudflare Workers AI"]
        if len(matches) != 1 or expected and matches[0].get("id") != expected:
            raise ReconcileError("A unique Cloudflare Workers AI credential is required")
        # A previously created n8n credential may predate the local manager
        # marker. Selecting it is an explicit owner action; encrypted contents
        # cannot be read or certified through the Public API.
        if expected is None:
            _save(record, {"credential_id": str(matches[0]["id"]),
                           "account_id": "unknown", "model": MODEL,
                           "binding_unverified": True})
    path = state_dir / "provider-selection.json"
    if path.is_symlink():
        raise ReconcileError("Provider selection path is unsafe")
    choices = json.loads(path.read_text()) if path.is_file() else \
        {"mika": "groq", "serious": "groq"}
    if set(choices) != {"mika", "serious"} or \
            any(value not in {"groq", "cloudflare"} for value in choices.values()):
        raise ReconcileError("Provider selection needs repair")
    choices[agent] = provider
    _save(path, choices)
    return {"status": "PROVIDER_SELECTED", "agent": agent, "provider": provider,
            "reason": "Reconcile the workflow; live inference and free quota still need verification"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "select", "status"))
    parser.add_argument("--origin", default="http://127.0.0.1:5678")
    parser.add_argument("--key-file", type=Path, default=Path("/opt/n8n/ai/api-key"))
    parser.add_argument("--state-dir", type=Path, default=Path("/opt/n8n/ai"))
    parser.add_argument("--account-id", default="")
    parser.add_argument("--agent", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--token-stdin", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            import getpass
            import sys
            token = sys.stdin.readline().strip() if args.token_stdin else \
                getpass.getpass("Cloudflare API token (hidden): ")
            result = prepare(args.origin, args.key_file, token, args.account_id,
                             args.state_dir)
        elif args.command == "select":
            result = select(args.origin, args.key_file, args.state_dir,
                            args.agent, args.provider)
        else:
            path = args.state_dir / "provider-selection.json"
            status_result = {"status": "CONFIGURED" if path.is_file() else "GROQ_DEFAULT",
                      "selection": json.loads(path.read_text()) if path.is_file() else
                      {"mika": "groq", "serious": "groq"},
                      "cloudflare_credential_prepared":
                      (args.state_dir / "cloudflare-ai.json").is_file()}
            print(json.dumps(status_result))
            return
        print(json.dumps(result))
    except (ReconcileError, OSError, ValueError) as exc:
        raise SystemExit("Cloudflare AI: " + str(exc)) from None


if __name__ == "__main__":
    main()
