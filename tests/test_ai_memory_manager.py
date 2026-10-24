"""Offline ownership and repeatability checks for managed AI PostgreSQL."""

import subprocess
import json
import os
from pathlib import Path

import pytest

from deploy.n8n.pg_memory import MemoryError, MemoryManager
from deploy.n8n.reconcile_ai import _attach_postgres_memory


def test_memory_install_is_repeatable_and_keeps_secrets(tmp_path, monkeypatch):
    manager = MemoryManager(tmp_path / "ai-memory")
    schema = Path(__file__).resolve().parents[1] / "deploy/n8n/ai_schema.sql"
    commands = []

    def run(*args, input_data=None, timeout=60):
        commands.append((args, input_data))
        output = b"1\n" if "-c" in args else b""
        return subprocess.CompletedProcess(args, 0, output, b"")

    monkeypatch.setattr(manager, "_run", run)
    assert manager.install(schema)["status"] == "DATABASE_READY"
    original = manager.env.read_bytes()
    assert manager.env.stat().st_mode & 0o777 == 0o600
    assert "ports:" not in manager.compose.read_text()
    assert "mikachan_n8n_default" in manager.compose.read_text()
    assert "\\getenv ai_pass AI_DB_PASSWORD" in (manager.root / "init/01-app.sh").read_text()
    assert 'ai_pass="$AI_DB_PASSWORD"' not in (manager.root / "init/01-app.sh").read_text()
    assert manager.install(schema)["status"] == "DATABASE_READY"
    assert manager.env.read_bytes() == original
    assert sum(bool(data and b"CREATE TABLE" in data) for _, data in commands) == 2


def test_memory_refuses_unowned_directory(tmp_path):
    root = tmp_path / "ai-memory"
    root.mkdir()
    manager = MemoryManager(root)
    with pytest.raises(MemoryError, match="not owned"):
        manager.install(Path(__file__).resolve().parents[1] / "deploy/n8n/ai_schema.sql")


def test_installed_memory_wires_independent_agent_tables(tmp_path):
    root = tmp_path / "ai-memory"
    root.mkdir()
    (root / ".managed-by-mikachan-ai-memory").write_text("Mikachan AI memory schema 1\n")
    (root / "secrets.env").write_text("AI_DB_PASSWORD=" + "x" * 40 + "\n")
    os.chmod(root / "secrets.env", 0o600)
    graph = json.loads((Path(__file__).resolve().parents[1] /
        "deploy/n8n/mikachan_ai_workflow.json").read_text())

    class Client:
        def request(self, method, path, body=None):
            if method == "GET":
                return {"data": []}
            assert method == "POST" and body["type"] == "postgres"
            return {"id": "pg-cred"}

    _attach_postgres_memory(Client(), graph, root)
    links = graph["connections"]
    assert links["Mika Postgres Memory"]["ai_memory"][0][0]["node"] == "AI Agent"
    assert links["Serious Postgres Memory"]["ai_memory"][0][0]["node"] == \
        "Mika Serious Agent"
    assert links["Mika Simple Memory"]["ai_memory"] == [[]]
    assert links["Serious Simple Memory"]["ai_memory"] == [[]]
    assert links["Decide AI"]["main"][0][0]["node"] == "Memory Intent"
    assert links["Write Explicit Memory"]["main"][0][0]["node"] == "Read Scoped Memory"
    assert links["Attach Scoped Memory"]["main"][0][0]["node"] == "Is AI Reply?"
    nodes = {node["name"]: node for node in graph["nodes"]}
    writer = nodes["Write Explicit Memory"]
    reader = nodes["Read Scoped Memory"]
    assert writer["credentials"]["postgres"]["id"] == "pg-cred"
    assert "ON CONFLICT DO NOTHING" in writer["parameters"]["query"]
    assert "visibility='private' AND $1=$2" in reader["parameters"]["query"]
    assert "source_kind='explicit_user'" in reader["parameters"]["query"]
    assert "m.chat?.type === 'private'" in nodes["Memory Intent"]["parameters"]["jsCode"]
    (root / "backend").write_text("simple\n")
    simple = json.loads((Path(__file__).resolve().parents[1] /
        "deploy/n8n/mikachan_ai_workflow.json").read_text())
    _attach_postgres_memory(Client(), simple, root)
    assert "Mika Postgres Memory" not in simple["connections"]


def test_failed_restore_attempts_automatic_rollback(tmp_path, monkeypatch):
    manager = MemoryManager(tmp_path / "ai-memory")
    backup_dir = manager.root / "backups"
    backup_dir.mkdir(parents=True)
    source = backup_dir / ("a" * 16 + ".dump")
    undo = backup_dir / ("b" * 16 + ".dump")
    source.write_bytes(b"PGDMPsource")
    undo.write_bytes(b"PGDMPundo")
    monkeypatch.setattr(manager, "backup", lambda: undo)
    calls = []

    def run(args, **kwargs):
        calls.append(kwargs["stdin"].name)
        return subprocess.CompletedProcess(args, 1 if len(calls) == 1 else 0, b"", b"")

    monkeypatch.setattr("deploy.n8n.pg_memory.subprocess.run", run)
    with pytest.raises(MemoryError, match="previous memory recovered"):
        manager.restore(source.name, "RESTORE AI MEMORY")
    assert calls == [str(source), str(undo)]
