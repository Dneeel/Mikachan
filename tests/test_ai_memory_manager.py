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
        output = b"accepting connections" if "pg_isready" in args else b""
        return subprocess.CompletedProcess(args, 0, output, b"")

    monkeypatch.setattr(manager, "_run", run)
    assert manager.install(schema)["status"] == "DATABASE_READY"
    original = manager.env.read_bytes()
    assert manager.env.stat().st_mode & 0o777 == 0o600
    assert "ports:" not in manager.compose.read_text()
    assert "mikachan_n8n_default" in manager.compose.read_text()
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
    (root / "backend").write_text("simple\n")
    simple = json.loads((Path(__file__).resolve().parents[1] /
        "deploy/n8n/mikachan_ai_workflow.json").read_text())
    _attach_postgres_memory(Client(), simple, root)
    assert "Mika Postgres Memory" not in simple["connections"]
