"""Safely reconcile a versioned AI workflow through the supported n8n Public API.

No key means no mutation. Existing graphs are never adopted on name alone:
their first reconciliation requires an identical graph or an explicit recorded
baseline. Every update is backed up and verified before its state is recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class ReconcileError(RuntimeError):
    pass


def _body(graph: dict[str, Any]) -> dict[str, Any]:
    return {key: graph[key] for key in ("name", "nodes", "connections", "settings")}


def _fingerprint(graph: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(_body(graph), ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _private_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ReconcileError("n8n API key file is unavailable")
    info = path.stat()
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
        raise ReconcileError("n8n API key file must be owned by the manager and mode 0600")
    value = path.read_text(encoding="utf-8").strip()
    if not 32 <= len(value) <= 4096 or "\n" in value:
        raise ReconcileError("n8n API key file is invalid")
    return value


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ai-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Client:
    def __init__(self, origin: str, key: str):
        parsed = urllib.parse.urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                parsed.path not in {"", "/"} or parsed.query or parsed.fragment or
                parsed.username or parsed.password or
                parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}):
            raise ReconcileError("n8n API origin must be HTTPS or private loopback")
        self.origin = origin.rstrip("/")
        self.key = key

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        request = urllib.request.Request(self.origin + "/api/v1" + path,
            data=payload, method=method,
            headers={"X-N8N-API-KEY": self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            # n8n error bodies can contain submitted values. Never print them.
            raise ReconcileError(f"n8n API {method} {path} failed") from exc
        if not isinstance(result, dict):
            raise ReconcileError("n8n API returned an unexpected response")
        return result


def _map_reply_table(client: Client, desired: dict[str, Any]) -> dict[str, str] | None:
    """Create or map the one known reply ledger without adopting unrelated tables."""
    nodes = [node for node in desired["nodes"] if node.get("type") == "n8n-nodes-base.dataTable"]
    if not nodes:
        return None
    if any(node.get("parameters", {}).get("dataTableId", {}).get("cachedResultName") !=
           "MikaAIReplies" for node in nodes):
        return {"status": "BLOCKED_CONFLICT", "reason": "Unexpected n8n data table reference"}
    rows = client.request("GET", "/data-tables?limit=100").get("data", [])
    matches = [row for row in rows if row.get("name") == "MikaAIReplies"]
    if len(matches) > 1:
        return {"status": "BLOCKED_CONFLICT", "reason": "Duplicate AI reply tables"}
    required = {"reply_key", "chat_id", "message_id", "session_id", "agent_type", "user_id"}
    if matches:
        table = client.request("GET", "/data-tables/" + str(matches[0]["id"]))
    else:
        table = client.request("POST", "/data-tables", {"name": "MikaAIReplies",
            "columns": [{"name": name, "type": "string"} for name in sorted(required)]})
    columns = {row.get("name") for row in table.get("columns", [])}
    if not required <= columns:
        return {"status": "NOT_CONFIGURED", "reason": "AI reply table columns need repair"}
    table_id = str(table["id"])
    for node in nodes:
        ref = node["parameters"]["dataTableId"]
        old_id = str(ref["value"])
        ref["value"] = table_id
        if isinstance(ref.get("cachedResultUrl"), str):
            ref["cachedResultUrl"] = ref["cachedResultUrl"].replace(old_id, table_id)
    return None


def reconcile(origin: str, key_file: Path, artifact: Path, state_dir: Path) -> dict[str, str]:
    if not key_file.is_file():
        return {"status": "NOT_CONFIGURED", "reason": "n8n API credential unavailable"}
    client = Client(origin, _private_file(key_file))
    desired = json.loads(artifact.read_text(encoding="utf-8"))
    state_file = state_dir / "workflow-state.json"
    previous = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
    listing = client.request("GET", "/workflows?limit=100").get("data", [])
    matches = [item for item in listing if item.get("name") == desired["name"]]
    if len(matches) > 1:
        return {"status": "BLOCKED_CONFLICT", "reason": "Duplicate named workflows"}
    current = client.request("GET", "/workflows/" + str(matches[0]["id"])) if matches else None
    conflict = False
    needed = {(kind, ref.get("name")) for node in desired["nodes"]
              for kind, ref in node.get("credentials", {}).items()}
    available = {(item.get("type"), item.get("name")):
                 item.get("id") for item in client.request("GET", "/credentials?limit=100").get("data", [])}
    missing = sorted(f"{kind}: {name}" for kind, name in needed if (kind, name) not in available)
    for node in desired["nodes"]:
        for kind, ref in list(node.get("credentials", {}).items()):
            pair = (kind, ref["name"])
            if pair in available:
                ref["id"] = available[pair]
            else:
                # An imported draft must never point at a credential ID from
                # another n8n instance. The owner attaches it in the editor.
                del node["credentials"][kind]
        if not node.get("credentials"):
            node.pop("credentials", None)
    table_problem = _map_reply_table(client, desired)
    if table_problem:
        return table_problem
    wanted = _fingerprint(desired)
    if current and _fingerprint(current) != wanted and (not previous or
            previous.get("id") != current.get("id") or
            previous.get("version_id") != current.get("versionId")):
        conflict = True
        # Never overwrite the owner's changed workflow. Import a separate draft.
        desired["name"] += " · Setup Draft"
        wanted = _fingerprint(desired)
        state_file = state_dir / "draft-state.json"
        previous = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
        drafts = [item for item in listing if item.get("name") == desired["name"]]
        if len(drafts) > 1:
            return {"status": "BLOCKED_CONFLICT", "reason": "Duplicate setup drafts"}
        current = client.request("GET", "/workflows/" + str(drafts[0]["id"])) if drafts else None
    if missing and current and current.get("active") and not conflict:
        return {"status": "NOT_CONFIGURED",
                "reason": "Active workflow was preserved; missing credentials: " + ", ".join(missing)}
    if current and _fingerprint(current) == wanted:
        _save(state_file, {"id": current["id"], "fingerprint": wanted,
                           "version_id": current.get("versionId")})
        return {"status": "IMPORTED_SETUP_REQUIRED" if conflict else
                "ACTIVE" if current.get("active") else
                "IMPORTED_SETUP_REQUIRED" if missing else "IMPORTED",
                "workflow_id": str(current["id"]),
                **({"reason": "Existing workflow preserved. Configure and activate the setup draft. " +
                    ("Missing: " + ", ".join(missing) if missing else "")}
                   if conflict else {"reason": "Attach credentials in the n8n editor, "
                    "then activate manually: " + ", ".join(missing)} if missing else {})}
    if conflict and current:
        return {"status": "DRAFT_EDITED", "workflow_id": str(current["id"]),
                "reason": "Existing setup draft was edited; it was preserved"}
    if current and (not previous or previous.get("id") != current.get("id") or
                    previous.get("version_id") != current.get("versionId")):
        return {"status": "BLOCKED_CONFLICT", "reason": "Existing workflow changed outside the reconciler"}
    if current:
        state_dir.mkdir(parents=True, exist_ok=True)
        _save(state_dir / ("backup-" + str(current["versionId"]) + ".json"), current)
    try:
        result = client.request("PUT", "/workflows/" + str(current["id"]), _body(desired)) \
            if current else client.request("POST", "/workflows", _body(desired))
        workflow_id = str(result["id"])
        if not missing and not conflict:
            client.request("POST", "/workflows/" + workflow_id + "/activate", {})
        installed = client.request("GET", "/workflows/" + workflow_id)
        if (bool(installed.get("active")) == bool(missing or conflict) or
                _fingerprint(installed) != _fingerprint(desired)):
            raise ReconcileError("Published n8n graph did not verify")
    except (ReconcileError, KeyError):
        if current:
            try:
                client.request("PUT", "/workflows/" + str(current["id"]), _body(current))
                if current.get("active"):
                    client.request("POST", "/workflows/" + str(current["id"]) + "/activate", {})
            except ReconcileError:
                raise ReconcileError("AI workflow update failed and automatic rollback needs repair") from None
        raise
    _save(state_file, {"id": workflow_id, "fingerprint": _fingerprint(desired),
                       "version_id": installed.get("versionId")})
    if conflict:
        return {"status": "IMPORTED_SETUP_REQUIRED", "workflow_id": workflow_id,
                "reason": "Existing workflow preserved. Configure and activate the setup draft. " +
                          ("Missing: " + ", ".join(missing) if missing else "")}
    if missing:
        return {"status": "IMPORTED_SETUP_REQUIRED", "workflow_id": workflow_id,
                "reason": "Attach credentials in the n8n editor, then activate manually: " +
                          ", ".join(missing)}
    return {"status": "ACTIVE", "workflow_id": workflow_id}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(reconcile(args.origin, args.key_file, args.artifact, args.state_dir)))


if __name__ == "__main__":
    main()
