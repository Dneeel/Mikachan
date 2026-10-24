"""Offline contract tests for the owner's two n8n agents and source assets."""

import json
import os
from pathlib import Path

from deploy.n8n.ai_assets import core_prompt, style_seeds
from deploy.n8n.build_ai_workflow import build
from deploy.n8n.seed_ai_style import seed_sql
from deploy.n8n.reconcile_ai import _fingerprint, _map_reply_table, reconcile


ARTIFACT = Path(__file__).resolve().parents[1] / "deploy/n8n/mikachan_ai_workflow.json"


def test_canonical_assets_and_seed_provenance():
    assert "دن" in core_prompt("chat") and "هورنی" in core_prompt("chat")
    assert "## IDENTITY, RELATIONSHIP, AND ROLE" in core_prompt("serious")
    seeds = style_seeds()
    assert len(seeds["chat"]["examples"]) == 40
    assert len(seeds["serious"]["examples"]) == 22
    assert all(not row["owner_approved"] for agent in ("chat", "serious")
               for row in seeds[agent]["examples"])


def test_artifact_has_two_isolated_agents_and_preserves_media_edges():
    graph = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    rebuilt = build(graph)
    assert len(rebuilt["nodes"]) == len(graph["nodes"])
    nodes = {node["name"]: node for node in graph["nodes"]}
    assert len([node for node in graph["nodes"] if node["type"] ==
                "n8n-nodes-base.telegramTrigger"]) == 0
    assert nodes["AI Agent"]["parameters"]["options"]["systemMessage"] != \
        nodes["Mika Serious Agent"]["parameters"]["options"]["systemMessage"]
    links = graph["connections"]
    assert links["Groq Chat Model"]["ai_languageModel"][0][0]["node"] == "AI Agent"
    assert links["Groq Serious Model"]["ai_languageModel"][0][0]["node"] == \
        "Mika Serious Agent"
    assert nodes["Groq Serious Model"]["parameters"]["model"] == \
        nodes["Groq Chat Model"]["parameters"]["model"]
    assert links["Mika Simple Memory"]["ai_memory"][0][0]["node"] == "AI Agent"
    assert links["Serious Simple Memory"]["ai_memory"][0][0]["node"] == \
        "Mika Serious Agent"
    assert links["AI Agent"]["main"][0][0]["node"] == "Normalize Chat Result"
    assert nodes["AI Agent"]["continueOnFail"] is True
    assert links["Tavily MCP"]["ai_tool"][0][0]["node"] == "AI Agent"
    assert links["Tavily MCP1"]["ai_tool"][0][0]["node"] == "Mika Serious Agent"
    assert len([n for n in graph["nodes"] if n["type"] == "n8n-nodes-base.stickyNote"]) == 8
    assert links["Get Mikasa"]["main"][0][0]["node"] == "Nude"
    assert links["Get Voice File"]["main"][0][0]["node"] == "TTS ElevenLabs"
    assert links["STT ElevenLabs"]["main"][0][0]["node"] == "Code in JavaScript"
    assert "reply_key" in nodes["Save AI Reply"]["parameters"]["columns"]["value"]
    assert "agent_type" in nodes["Save AI Reply"]["parameters"]["columns"]["value"]


def test_style_seed_sql_is_idempotent_and_preserves_review_state():
    sql = seed_sql()
    assert sql.count("INSERT INTO mika_style_rules") == 24
    assert sql.count("INSERT INTO mika_style_examples") == 62
    assert sql.count("ON CONFLICT DO NOTHING") == 86
    assert "owner_approved" not in sql


def test_reconcile_never_contacts_n8n_without_private_api_key(tmp_path):
    result = reconcile("https://n8n.example.org", tmp_path / "absent",
                       ARTIFACT, tmp_path / "state")
    assert result["status"] == "NOT_CONFIGURED"
    assert not (tmp_path / "state").exists()
    graph = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert _fingerprint(graph) == _fingerprint({**graph, "id": "ignored"})


def test_reconcile_maps_credentials_before_comparing_existing_graph(tmp_path, monkeypatch):
    desired = {"name": "AI", "nodes": [{"name": "Model",
        "credentials": {"groqApi": {"name": "Groq account", "id": "old-id"}}}],
        "connections": {}, "settings": {}}
    artifact = tmp_path / "workflow.json"
    artifact.write_text(json.dumps(desired))
    key_file = tmp_path / "api-key"
    key_file.write_text("x" * 32)
    os.chmod(key_file, 0o600)
    current = json.loads(json.dumps(desired))
    current["nodes"][0]["credentials"]["groqApi"]["id"] = "new-id"
    current.update(id="existing", versionId="v1", active=True)

    class FakeClient:
        def __init__(self, _origin, _key):
            pass

        def request(self, method, path, body=None):
            assert method == "GET", "An identical graph must not be changed"
            if path.startswith("/workflows?"):
                return {"data": [{"id": "existing", "name": "AI"}]}
            if path == "/workflows/existing":
                return current
            if path.startswith("/credentials?"):
                return {"data": [{"type": "groqApi", "name": "Groq account",
                                  "id": "new-id"}]}
            raise AssertionError(path)

    monkeypatch.setattr("deploy.n8n.reconcile_ai.Client", FakeClient)
    result = reconcile("https://n8n.example.org", key_file, artifact, tmp_path / "state")
    assert result == {"status": "ACTIVE", "workflow_id": "existing"}


def test_reply_table_is_created_and_remapped_on_new_instance():
    graph = {"nodes": [{"type": "n8n-nodes-base.dataTable", "parameters": {
        "dataTableId": {"value": "old", "cachedResultName": "MikaAIReplies",
                        "cachedResultUrl": "/data/old"}}}]}

    class Client:
        def request(self, method, path, body=None):
            if method == "GET":
                return {"data": []}
            assert method == "POST" and path == "/data-tables"
            return {"id": "new", "columns": body["columns"]}

    assert _map_reply_table(Client(), graph) is None
    ref = graph["nodes"][0]["parameters"]["dataTableId"]
    assert ref["value"] == "new" and ref["cachedResultUrl"] == "/data/new"


def test_missing_provider_imports_inactive_draft_without_foreign_credential(tmp_path, monkeypatch):
    graph = {"name": "AI", "nodes": [{"name": "Model", "credentials": {
        "groqApi": {"name": "Groq account", "id": "foreign-id"}}}],
        "connections": {}, "settings": {}}
    artifact = tmp_path / "workflow.json"
    artifact.write_text(json.dumps(graph))
    key_file = tmp_path / "api-key"
    key_file.write_text("x" * 32)
    os.chmod(key_file, 0o600)

    class FakeClient:
        installed = None

        def __init__(self, _origin, _key):
            pass

        def request(self, method, path, body=None):
            if path.startswith("/workflows?"):
                return {"data": []}
            if path.startswith("/credentials?"):
                return {"data": []}
            if path.startswith("/data-tables?"):
                return {"data": []}
            if method == "POST" and path == "/workflows":
                assert "credentials" not in body["nodes"][0]
                FakeClient.installed = {**body, "id": "new", "active": False,
                                        "versionId": "v1"}
                return {"id": "new"}
            if method == "GET" and path == "/workflows/new":
                return FakeClient.installed
            raise AssertionError((method, path))  # Activation must not be attempted.

    monkeypatch.setattr("deploy.n8n.reconcile_ai.Client", FakeClient)
    result = reconcile("https://n8n.example.org", key_file, artifact, tmp_path / "state")
    assert result["status"] == "IMPORTED_SETUP_REQUIRED"
    assert result["workflow_id"] == "new"
    assert "Groq account" in result["reason"]


def test_changed_existing_workflow_is_preserved_and_separate_draft_imported(tmp_path, monkeypatch):
    graph = {"name": "AI", "nodes": [], "connections": {}, "settings": {}}
    artifact = tmp_path / "workflow.json"
    artifact.write_text(json.dumps(graph))
    key_file = tmp_path / "api-key"
    key_file.write_text("x" * 32)
    os.chmod(key_file, 0o600)
    existing = {**graph, "settings": {"manual": True}, "id": "owner",
                "versionId": "owner-v2", "active": True}

    class FakeClient:
        draft = None

        def __init__(self, _origin, _key):
            pass

        def request(self, method, path, body=None):
            if path.startswith("/workflows?"):
                data = [{"id": "owner", "name": "AI"}]
                if FakeClient.draft:
                    data.append({"id": "draft", "name": "AI · Setup Draft"})
                return {"data": data}
            if path == "/workflows/owner":
                return existing
            if path == "/workflows/draft":
                return FakeClient.draft
            if path.startswith("/credentials?"):
                return {"data": []}
            if method == "POST" and path == "/workflows":
                assert body["name"] == "AI · Setup Draft"
                FakeClient.draft = {**body, "id": "draft", "versionId": "draft-v1",
                                    "active": False}
                return {"id": "draft"}
            raise AssertionError((method, path))

    monkeypatch.setattr("deploy.n8n.reconcile_ai.Client", FakeClient)
    first = reconcile("https://n8n.example.org", key_file, artifact, tmp_path / "state")
    assert first["status"] == "IMPORTED_SETUP_REQUIRED"
    assert first["workflow_id"] == "draft"
    assert existing["versionId"] == "owner-v2"
    second = reconcile("https://n8n.example.org", key_file, artifact, tmp_path / "state")
    assert second["workflow_id"] == "draft"
