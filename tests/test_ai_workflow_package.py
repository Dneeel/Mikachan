"""Offline contract tests for the owner's two n8n agents and source assets."""

import json
import os
import copy
from pathlib import Path

import pytest

from deploy.n8n.ai_assets import core_prompt, style_seeds
from deploy.n8n.build_ai_workflow import build
from deploy.n8n.seed_ai_style import seed_sql
from deploy.n8n.reconcile_ai import (ReconcileError, _activate_draft,
    _attach_search_fallback, _draft_missing_credentials, _fingerprint,
    _map_reply_table, reconcile)


ARTIFACT = Path(__file__).resolve().parents[1] / "deploy/n8n/mikachan_ai_workflow.json"


def test_ready_draft_requires_real_credentials_and_private_webhook():
    hook = {"name": "Private Webhook", "type": "n8n-nodes-base.webhook",
            "parameters": {"path": "mikachan-relay", "httpMethod": "POST",
                           "authentication": "headerAuth"},
            "credentials": {"httpHeaderAuth": {"id": "secret-id"}}}
    agent = {"name": "AI Agent", "type": "agent",
             "credentials": {"groqApi": {"id": "model-id"}}}
    desired = {"nodes": [hook, agent]}
    available = [{"type": "httpHeaderAuth", "id": "secret-id"},
                 {"type": "groqApi", "id": "model-id"}]
    required = {"Private Webhook": {"httpHeaderAuth"}, "AI Agent": {"groqApi"}}
    assert _draft_missing_credentials(desired, desired, available, required) == []
    without_model = {"nodes": [hook, {"name": "AI Agent", "type": "agent"}]}
    assert _draft_missing_credentials(without_model, desired, available, required) == [
        "AI Agent credential"]
    wrong_path = copy.deepcopy(desired)
    wrong_path["nodes"][0]["parameters"]["path"] = "other"
    assert any("POST /webhook/mikachan-relay" in reason for reason in
               _draft_missing_credentials(wrong_path, desired, available, required))


@pytest.mark.parametrize("fail_activation", [False, True])
def test_draft_cutover_verifies_or_restores_original(tmp_path, fail_activation):
    original = {"id": "old", "active": True, "name": "AI", "nodes": [],
                "connections": {}, "settings": {}}
    draft = {**original, "id": "new", "active": False, "name": "AI · Setup Draft"}
    calls = []

    class Client:
        def request(self, method, path, _body=None):
            calls.append((method, path))
            if path == "/workflows/new" and method == "GET":
                return {**draft, "active": not fail_activation, "versionId": "v2"}
            if path == "/workflows/new/activate" and fail_activation:
                raise ReconcileError("activation failed")
            return {}

    if fail_activation:
        with pytest.raises(ReconcileError):
            _activate_draft(Client(), original, draft, tmp_path, tmp_path / "state.json")
        assert ("POST", "/workflows/old/activate") in calls
        assert not (tmp_path / "state.json").exists()
    else:
        assert _activate_draft(Client(), original, draft, tmp_path,
                               tmp_path / "state.json") == {"status": "ACTIVE", "workflow_id": "new"}
        assert calls[:2] == [("POST", "/workflows/old/deactivate"),
                             ("POST", "/workflows/new/activate")]
        assert (tmp_path / "cutover-original-backup.json").is_file()


def test_reconcile_repairs_credentials_and_activates_existing_draft(tmp_path, monkeypatch):
    webhook = {"name": "Private Webhook", "type": "n8n-nodes-base.webhook",
               "parameters": {"path": "mikachan-relay", "httpMethod": "POST",
                              "authentication": "headerAuth"},
               "credentials": {"httpHeaderAuth": {"name": "Mikachan Private Relay",
                                                   "id": "foreign"}}}
    model = {"name": "Model", "type": "model",
             "credentials": {"groqApi": {"name": "Groq account", "id": "foreign"}}}
    graph = {"name": "AI", "nodes": [webhook, model], "connections": {}, "settings": {}}
    artifact = tmp_path / "workflow.json"
    artifact.write_text(json.dumps(graph))
    key = tmp_path / "api-key"
    key.write_text("x" * 40)
    os.chmod(key, 0o600)
    original = {**graph, "settings": {"owner_note": True}, "id": "original",
                "active": True, "versionId": "old-v1"}
    draft = {"name": "AI · Setup Draft", "nodes": [
        {k: v for k, v in webhook.items() if k != "credentials"},
        {k: v for k, v in model.items() if k != "credentials"}],
        "connections": {}, "settings": {}, "id": "draft", "active": False,
        "versionId": "draft-v1"}
    events = []

    class Client:
        def __init__(self, _origin, _key):
            pass

        def request(self, method, path, body=None):
            events.append((method, path))
            if path.startswith("/workflows?"):
                return {"data": [{"id": "original", "name": "AI"},
                                 {"id": "draft", "name": "AI · Setup Draft"}]}
            if path.startswith("/credentials?"):
                return {"data": [{"type": "httpHeaderAuth", "name": "Mikachan Private Relay",
                                  "id": "header-id"},
                                 {"type": "groqApi", "name": "Groq account", "id": "groq-id"}]}
            if path == "/workflows/original" and method == "GET":
                return original
            if path == "/workflows/draft" and method == "GET":
                return draft
            if path == "/workflows/draft" and method == "PUT":
                draft.update(body, versionId="draft-v2")
                return {"id": "draft"}
            if path == "/workflows/original/deactivate":
                original["active"] = False
                return {}
            if path == "/workflows/draft/activate":
                draft["active"] = True
                return {}
            raise AssertionError((method, path))

    monkeypatch.setattr("deploy.n8n.reconcile_ai.Client", Client)
    result = reconcile("https://n8n.example.org", key, artifact, tmp_path / "state")
    assert result == {"status": "ACTIVE", "workflow_id": "draft"}
    assert original["active"] is False and draft["active"] is True
    assert draft["nodes"][0]["credentials"]["httpHeaderAuth"]["id"] == "header-id"
    assert draft["nodes"][1]["credentials"]["groqApi"]["id"] == "groq-id"
    assert events.index(("POST", "/workflows/original/deactivate")) < \
        events.index(("POST", "/workflows/draft/activate"))


def test_canonical_assets_and_seed_provenance():
    assert "دن" in core_prompt("chat") and "هورنی" in core_prompt("chat")
    assert "## IDENTITY, RELATIONSHIP, AND ROLE" in core_prompt("serious")
    assert "verifiedOwner=true" in core_prompt("chat")
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
    assert "verifiedOwner: $json.message.verified_owner === true" in \
        nodes["AI Agent"]["parameters"]["options"]["systemMessage"]
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
    for name in ("Cloudflare Mika Model", "Cloudflare Serious Model"):
        assert nodes[name]["disabled"] is True
        assert nodes[name]["parameters"]["model"]["value"] == \
            "@cf/google/gemma-4-26b-a4b-it"
        assert name not in links or not links[name].get("ai_languageModel")
    assert links["Get Mikasa"]["main"][0][0]["node"] == "Nude"
    assert links["Get Voice File"]["main"][0][0]["node"] == "STT ElevenLabs"
    assert links["STT ElevenLabs"]["main"][0][0]["node"] == "Elevenlabs STT"
    assert links["TTS ElevenLabs"]["main"][0][0]["node"] == "Code in JavaScript"
    assert "reply_key" in nodes["Save AI Reply"]["parameters"]["columns"]["value"]
    assert "agent_type" in nodes["Save AI Reply"]["parameters"]["columns"]["value"]


def test_existing_search_fallback_query_values_are_repaired():
    graph = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    _attach_search_fallback(graph)
    nodes = {node["name"]: node for node in graph["nodes"]}
    for label in ("Mika", "Serious"):
        for kind in ("Wikipedia", "News"):
            rows = nodes[f"{label} {kind} Fallback"]["parameters"]["parametersQuery"]["values"]
            assert rows and all("value" in row and row["value"] for row in rows)


def test_reconcile_repairs_known_search_query_gap_without_draft(tmp_path, monkeypatch):
    graph = {"name": "AI", "nodes": [
        {"name": name, "parameters": {"options": {"systemMessage": "Search carefully."}}}
        for name in ("AI Agent", "Mika Serious Agent")],
        "connections": {}, "settings": {}}
    _attach_search_fallback(graph)
    current = copy.deepcopy(graph)
    current["id"] = "existing"
    current["versionId"] = "v1"
    current["active"] = True
    next(node for node in current["nodes"] if node["name"] ==
         "Serious News Fallback")["parameters"]["parametersQuery"]["values"][0].pop("value")
    artifact = tmp_path / "workflow.json"
    artifact.write_text(json.dumps(graph))
    key = tmp_path / "api-key"
    key.write_text("k" * 40)
    os.chmod(key, 0o600)

    class Client:
        def __init__(self, _origin, _key):
            pass

        def request(self, method, path, body=None):
            if method == "GET" and path.startswith("/workflows?"):
                return {"data": [{"id": "existing", "name": "AI"}]}
            if method == "GET" and path.startswith("/credentials?"):
                return {"data": []}
            if method == "GET" and path == "/workflows/existing":
                return current
            if method == "PUT" and path == "/workflows/existing":
                assert body["name"] == "AI"
                current.update(body, versionId="v2")
                return {"id": "existing"}
            if method == "POST" and path == "/workflows/existing/activate":
                return {}
            raise AssertionError((method, path))

    monkeypatch.setattr("deploy.n8n.reconcile_ai.Client", Client)
    result = reconcile("https://n8n.example.org", key, artifact, tmp_path / "state")
    assert result["status"] == "ACTIVE"
    assert result["workflow_id"] == "existing"


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


def test_disabled_optional_model_does_not_block_active_workflow(tmp_path, monkeypatch):
    graph = {"name": "AI", "nodes": [{"name": "Optional Model", "disabled": True,
        "credentials": {"openAiApi": {"name": "Cloudflare Workers AI", "id": "foreign"}}}],
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
            if path.startswith("/workflows?") or path.startswith("/credentials?") or \
                    path.startswith("/data-tables?"):
                return {"data": []}
            if method == "POST" and path == "/workflows":
                FakeClient.installed = {**body, "id": "new", "active": False,
                                        "versionId": "v1"}
                return {"id": "new"}
            if method == "POST" and path == "/workflows/new/activate":
                FakeClient.installed["active"] = True
                return {}
            if method == "GET" and path == "/workflows/new":
                return FakeClient.installed
            raise AssertionError((method, path))

    monkeypatch.setattr("deploy.n8n.reconcile_ai.Client", FakeClient)
    result = reconcile("https://n8n.example.org", key_file, artifact, tmp_path / "state")
    assert result["status"] == "ACTIVE"


def test_search_fallback_keeps_tavily_and_has_bounded_public_sources():
    from deploy.n8n.reconcile_ai import _attach_search_fallback

    graph = json.loads((Path(__file__).resolve().parents[1] /
        "deploy/n8n/mikachan_ai_workflow.json").read_text())
    _attach_search_fallback(graph)
    tools = {node["name"]: node for node in graph["nodes"]}
    assert "Tavily MCP" in tools
    for label in ("Mika", "Serious"):
        for source in ("Wikipedia", "News"):
            name = f"{label} {source} Fallback"
            assert tools[name]["parameters"].get("authentication", "none") == "none"
            assert graph["connections"][name]["ai_tool"][0][0]["node"] == \
                ("AI Agent" if label == "Mika" else "Mika Serious Agent")
            assert tools[name]["parameters"]["placeholderDefinitions"]["values"][0]["name"] == "query"
    assert tools["Mika News Fallback"]["parameters"]["maxLength"] == 8000


def test_ai_telegram_replies_use_original_webhook_message_after_agent():
    graph = json.loads((Path(__file__).resolve().parents[1] /
        "deploy/n8n/mikachan_ai_workflow.json").read_text())
    nodes = {node["name"]: node for node in graph["nodes"]}
    for name in ("Send a text message", "Send Serious Reply"):
        params = nodes[name]["parameters"]
        assert "Telegram Trigger').first()" in params["chatId"]
        assert "Telegram Trigger').first()" in \
            params["additionalFields"]["reply_to_message_id"]
        assert params.get("operation", "sendMessage") != "sendMessageDraft"
    assert nodes["Mika Simple Memory"]["parameters"]["contextWindowLength"] == 8
    assert nodes["Serious Simple Memory"]["parameters"]["contextWindowLength"] == 8


def test_verified_base_graph_gets_additive_memory_without_replacing_owner_edits(
        tmp_path, monkeypatch):
    graph = {"name": "AI", "nodes": [], "connections": {}, "settings": {}}
    artifact = tmp_path / "workflow.json"
    artifact.write_text(json.dumps(graph))
    key = tmp_path / "api-key"
    key.write_text("k" * 40)
    os.chmod(key, 0o600)
    current = {**graph, "id": "owned", "versionId": "owner-v2", "active": True}

    class Client:
        saved = current
        updated = False

        def __init__(self, _origin, _key):
            pass

        def request(self, method, path, body=None):
            if path.startswith("/workflows?"):
                return {"data": [{"id": "owned", "name": "AI"}]}
            if path == "/workflows/owned" and method == "GET":
                return Client.saved
            if path.startswith("/credentials?") or path.startswith("/data-tables?"):
                return {"data": []}
            if path == "/workflows/owned" and method == "PUT":
                Client.updated = True
                Client.saved = {**body, "id": "owned", "versionId": "v3", "active": True}
                return {"id": "owned"}
            if path == "/workflows/owned/activate" and method == "POST":
                return {}
            raise AssertionError((method, path))

    def add_memory(_client, desired, _root):
        desired["nodes"].append({"name": "Managed Memory", "type": "memory", "parameters": {}})

    monkeypatch.setattr("deploy.n8n.reconcile_ai.Client", Client)
    monkeypatch.setattr("deploy.n8n.reconcile_ai._attach_postgres_memory", add_memory)
    result = reconcile("https://n8n.example.org", key, artifact, tmp_path / "state")
    assert result["status"] == "ACTIVE" and Client.updated
    assert [node["name"] for node in Client.saved["nodes"]] == ["Managed Memory"]


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
