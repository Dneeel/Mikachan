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
    _configure_search, _configure_tts_v4, _draft_missing_credentials, _fingerprint,
    _owner_graph, _preserve_owner_settings, _repair_active_search, _select_models,
    _map_reply_table, reconcile)


ARTIFACT = Path(__file__).resolve().parents[1] / "deploy/n8n/mikachan_ai_workflow.json"


def test_packaged_ai_handles_first_reply_and_captioned_media():
    graph = json.loads(ARTIFACT.read_text())
    nodes = {node["name"]: node for node in graph["nodes"]}
    decide = nodes["Decide AI"]["parameters"]["jsCode"]
    analyze = nodes["Analyze Message"]["parameters"]["jsCode"]
    assert "found ? String(found.json.agent_type || 'chat') : 'chat'" in decide
    assert "m.reply_to_message?.text?.trim() || m.reply_to_message?.caption?.trim()" in analyze


def test_search_reconnects_after_long_term_memory_is_added():
    graph = json.loads(ARTIFACT.read_text())
    graph["nodes"].append({"name": "Attach Scoped Memory", "type": "n8n-nodes-base.code",
                           "parameters": {"jsCode": "return $input.all();"}})
    graph["connections"]["Decide AI"]["main"] = [[{"node": "Attach Scoped Memory",
        "type": "main", "index": 0}]]
    graph["connections"]["Attach Scoped Memory"] = {"main": [[{
        "node": "Is AI Reply?", "type": "main", "index": 0}]]}
    _configure_search(graph, "search-credential")
    assert graph["connections"]["Attach Scoped Memory"]["main"][0][0]["node"] == \
        "Search Intent"
    assert graph["connections"]["Search Intent"]["main"][0][0]["node"] == \
        "Prepare Search Query"
    assert graph["connections"]["Tavily HTTP Search"]["main"][0][0]["node"] == \
        "Attach Search Results"
    assert graph["connections"]["Attach Search Results"]["main"][0][0]["node"] == \
        "Is News Result?"
    assert graph["connections"]["Is News Result?"]["main"][0][0]["node"] == \
        "Render Sourced Headlines"
    assert graph["connections"]["Is News Result?"]["main"][1][0]["node"] == \
        "Is AI Reply?"
    assert graph["connections"]["Render Sourced Headlines"]["main"][0][0]["node"] == \
        "Send a text message"
    first = _fingerprint(graph)
    _configure_search(graph, "search-credential")
    assert _fingerprint(graph) == first


def test_v4_tts_repairs_legacy_node_and_preserves_owner_voice():
    graph = json.loads(ARTIFACT.read_text())
    graph["nodes"] = [n for n in graph["nodes"] if n["name"] != "Prepare TTS Text"]
    graph["connections"].pop("Prepare TTS Text")
    graph["connections"]["Switch"]["main"][1] = [{"node": "TTS ElevenLabs",
        "type": "main", "index": 0}]
    node = next(n for n in graph["nodes"] if n["name"] == "TTS ElevenLabs")
    node["type"] = "@elevenlabs/n8n-nodes-elevenlabs.elevenLabs"
    node["parameters"] = {"resource": "speech", "voice": {"value": "ownerVoice12345"},
        "additionalOptions": {"model": {"value": ""},
                              "outputFormat": "mp3_44100_128"}}
    _configure_tts_v4(graph)
    assert node["type"] == "n8n-nodes-base.httpRequest"
    assert "/ownerVoice12345?" in node["parameters"]["url"]
    assert node["parameters"]["nodeCredentialType"] == "elevenLabsApi"
    assert node["parameters"]["jsonBody"] == \
        "={{ JSON.stringify($json.ttsRequest) }}"
    assert graph["connections"]["Switch"]["main"][1][0]["node"] == \
        "Prepare TTS Text"
    first = _fingerprint(graph)
    _configure_tts_v4(graph)
    assert _fingerprint(graph) == first


def test_owner_postgres_template_keeps_media_voice_and_memory_when_repaired():
    """The supplied 50-node export uses Postgres memory and owner TTS settings."""
    graph = json.loads(ARTIFACT.read_text())
    nodes = {node["name"]: node for node in graph["nodes"]}
    for name, agent in (("Mika Postgres Memory", "AI Agent"),
                        ("Serious Postgres Memory", "Mika Serious Agent")):
        graph["nodes"].append({"name": name, "type":
            "@n8n/n8n-nodes-langchain.memoryPostgresChat", "parameters": {
                "sessionIdType": "customKey", "sessionKey": "={{ $json.mika.sessionId }}"}})
        graph["connections"][name] = {"ai_memory": [[{"node": agent,
            "type": "ai_memory", "index": 0}]]}
    graph["connections"].pop("Mika Simple Memory", None)
    graph["connections"].pop("Serious Simple Memory", None)
    graph["nodes"] = [node for node in graph["nodes"] if node["name"] not in
                      {"Mika Simple Memory", "Serious Simple Memory"}]
    nodes["TTS ElevenLabs"]["parameters"]["voice"] = {
        "__rl": True, "value": "owner-voice", "mode": "id"}
    repaired = build(graph)
    _configure_search(repaired, "search-credential")
    kept = {node["name"]: node for node in repaired["nodes"]}
    assert kept["TTS ElevenLabs"]["parameters"]["voice"]["value"] == "owner-voice"
    assert all(name in kept for name in ("Mika Postgres Memory", "Serious Postgres Memory",
        "Get Mikasa", "STT ElevenLabs", "TTS ElevenLabs"))
    assert repaired["connections"]["Get Voice File"] == graph["connections"]["Get Voice File"]


def test_reconcile_preserves_owner_voice_prompt_note_and_known_credentials():
    desired = json.loads(ARTIFACT.read_text())
    current = copy.deepcopy(desired)
    wanted = {node["name"]: node for node in desired["nodes"]}
    live = {node["name"]: node for node in current["nodes"]}
    live["TTS ElevenLabs"]["parameters"]["voice"] = {
        "__rl": True, "value": "private-owner-voice", "mode": "id"}
    live["AI Agent"]["parameters"]["options"]["systemMessage"] = "owner prompt"
    live["Sticky Note12"]["parameters"]["content"] = "owner note"
    live["TTS ElevenLabs"]["credentials"] = {
        "elevenLabsApi": {"id": "live-id", "name": "Owner ElevenLabs"}}
    _preserve_owner_settings(desired, current, [{"type": "elevenLabsApi", "id": "live-id",
                                               "name": "Owner ElevenLabs"}])
    assert wanted["TTS ElevenLabs"]["parameters"]["voice"]["value"] == \
        "private-owner-voice"
    assert wanted["TTS ElevenLabs"]["credentials"]["elevenLabsApi"]["id"] == "live-id"
    assert wanted["AI Agent"]["parameters"]["options"]["systemMessage"] == "owner prompt"
    assert wanted["Sticky Note12"]["parameters"]["content"] == "owner note"


def test_owner_workflow_is_repairable_in_place_only_with_authenticated_hook():
    current = json.loads(ARTIFACT.read_text())
    current["id"] = "owner-workflow"
    current["active"] = True
    current["versionId"] = "owner-version"
    graph = _owner_graph(current)
    assert graph is not None and graph["name"] == current["name"]
    assert "id" not in graph and "active" not in graph
    broken = copy.deepcopy(current)
    hook = next(node for node in broken["nodes"] if node["name"] == "Private Webhook")
    hook["parameters"]["authentication"] = "none"
    assert _owner_graph(broken) is None


def test_valid_owner_workflow_repairs_same_id_without_creating_draft(tmp_path,
                                                                     monkeypatch):
    current = json.loads(ARTIFACT.read_text())
    current.update(id="owner-id", active=True, versionId="owner-v1")
    owner_nodes = {node["name"]: node for node in current["nodes"]}
    owner_nodes["Analyze Message"]["parameters"]["jsCode"] = "return []; // old bug"
    owner_nodes["TTS ElevenLabs"]["parameters"]["voice"] = {
        "__rl": True, "value": "owner-voice", "mode": "id"}
    credential_rows = [{"type": kind, "name": ref["name"], "id": ref["id"]}
        for node in current["nodes"] for kind, ref in node.get("credentials", {}).items()]
    events = []

    class Client:
        def __init__(self, _origin, _key):
            pass

        def request(self, method, path, body=None):
            events.append((method, path))
            if path == "/workflows?limit=100":
                return {"data": [{"name": current["name"], "id": "owner-id"}]}
            if path == "/workflows/owner-id" and method == "GET":
                return copy.deepcopy(current)
            if path == "/credentials?limit=100":
                return {"data": credential_rows}
            if path == "/workflows/owner-id" and method == "PUT":
                current.update(copy.deepcopy(body), versionId="owner-v2")
                return {"id": "owner-id"}
            if path == "/workflows/owner-id/activate":
                current["active"] = True
                return {}
            raise AssertionError((method, path))

    monkeypatch.setattr("deploy.n8n.reconcile_ai.Client", Client)
    monkeypatch.setattr("deploy.n8n.reconcile_ai._map_reply_table",
                        lambda *_args: None)
    monkeypatch.setattr("deploy.n8n.reconcile_ai._attach_postgres_memory",
                        lambda *_args: None)
    artifact = tmp_path / "workflow.json"
    artifact.write_text(ARTIFACT.read_text())
    key = tmp_path / "key"
    key.write_text("k" * 40)
    key.chmod(0o600)
    result = reconcile("http://127.0.0.1:5678", key, artifact, tmp_path / "state")
    assert result["status"] == "ACTIVE"
    assert result["workflow_id"] == "owner-id"
    assert ("PUT", "/workflows/owner-id") in events
    assert not any(method == "POST" and path == "/workflows" for method, path in events)
    assert next(node for node in current["nodes"] if node["name"] ==
                "TTS ElevenLabs")["parameters"]["voice"]["value"] == "owner-voice"


def test_cloudflare_selection_uses_http_request_without_replacing_groq(tmp_path):
    graph = json.loads(ARTIFACT.read_text())
    selected = tmp_path / "provider-selection.json"
    selected.write_text(json.dumps({"mika": "cloudflare", "serious": "groq"}))
    selected.chmod(0o600)
    marker = tmp_path / "cloudflare-ai.json"
    marker.write_text(json.dumps({"account_id": "a" * 32, "credential_id": "cf-cred"}))
    marker.chmod(0o600)
    _select_models(graph, tmp_path)
    nodes = {node["name"]: node for node in graph["nodes"]}
    assert nodes["Cloudflare Mika Model"]["type"] == "n8n-nodes-base.httpRequest"
    assert "/ai/run/@cf/google/gemma-4-26b-a4b-it" in \
        nodes["Cloudflare Mika Model"]["parameters"]["url"]
    assert nodes["Cloudflare Mika Model"]["credentials"]["httpHeaderAuth"]["id"] == "cf-cred"
    assert graph["connections"]["Is AI Reply?"]["main"][0][0]["node"] == \
        "Cloudflare Mika Model"
    assert graph["connections"]["Is AI Reply?"]["main"][1][0]["node"] == \
        "Mika Serious Agent"
    assert graph["connections"]["Cloudflare Mika Result"]["main"][0][0]["node"] == \
        "Normalize Chat Result"


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
    postgres_draft = {"nodes": [hook, agent,
        {"name": "Mika Postgres Memory", "type": "memory"},
        {"name": "Serious Postgres Memory", "type": "memory"}]}
    with_old_memory = {"nodes": [*desired["nodes"],
        {"name": "Mika Simple Memory", "type": "memory"},
        {"name": "Serious Simple Memory", "type": "memory"}]}
    assert _draft_missing_credentials(postgres_draft, with_old_memory, available,
                                      required) == []


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
    original = {**graph, "nodes": copy.deepcopy(graph["nodes"]),
                "settings": {"owner_note": True}, "id": "original",
                "active": True, "versionId": "old-v1"}
    original["nodes"][1]["credentials"]["groqApi"] = {
        "name": "Owner selected Groq", "id": "owner-groq-id"}
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
                                 {"type": "groqApi", "name": "Groq account", "id": "groq-id"},
                                 {"type": "groqApi", "name": "Owner selected Groq",
                                  "id": "owner-groq-id"}]}
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
    result = reconcile("https://n8n.example.org", key, artifact, tmp_path / "state",
                       memory_root=tmp_path / "memory")
    assert result == {"status": "ACTIVE", "workflow_id": "draft"}
    assert original["active"] is False and draft["active"] is True
    assert draft["nodes"][0]["credentials"]["httpHeaderAuth"]["id"] == "header-id"
    assert draft["nodes"][1]["credentials"]["groqApi"]["id"] == "owner-groq-id"
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
    analyze = nodes["Analyze Message"]["parameters"]["jsCode"]
    assert "cleanText || fallback" in analyze
    assert "only addressed you by name" in analyze
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
    assert "Tavily MCP" not in links and "Tavily MCP1" not in links
    assert links["Search Intent"]["main"][0][0]["node"] == "Prepare Search Query"
    assert links["Search Intent"]["main"][1][0]["node"] == "Is AI Reply?"
    assert links["Prepare Search Query"]["main"][0][0]["node"] == "Tavily HTTP Search"
    assert links["Tavily HTTP Search"]["main"][0][0]["node"] == "Attach Search Results"
    assert len([n for n in graph["nodes"] if n["type"] == "n8n-nodes-base.stickyNote"]) == 8
    for name in ("Cloudflare Mika Model", "Cloudflare Serious Model"):
        assert nodes[name]["disabled"] is True
        assert nodes[name]["type"] == "n8n-nodes-base.httpRequest"
        assert name not in links or not links[name].get("ai_languageModel")
    assert links["Get Mikasa"]["main"][0][0]["node"] == "Nude"
    assert links["Get Voice File"]["main"][0][0]["node"] == "STT ElevenLabs"
    assert links["STT ElevenLabs"]["main"][0][0]["node"] == "Elevenlabs STT"
    assert links["TTS ElevenLabs"]["main"][0][0]["node"] == "Code in JavaScript"
    assert "reply_key" in nodes["Save AI Reply"]["parameters"]["columns"]["value"]
    assert "agent_type" in nodes["Save AI Reply"]["parameters"]["columns"]["value"]


def test_single_search_removes_legacy_tools_and_is_idempotent():
    graph = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    graph["nodes"].append({"name": "Mika News Fallback", "type": "legacy"})
    graph["connections"]["Mika News Fallback"] = {"ai_tool": [[]]}
    _configure_search(graph)
    assert "Mika News Fallback" not in {n["name"] for n in graph["nodes"]}
    before = _fingerprint(graph)
    _configure_search(graph)
    assert _fingerprint(graph) == before


def test_active_draft_single_search_update_is_verified(tmp_path):
    draft = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    draft.update(id="draft", active=True, versionId="old")
    draft["nodes"].append({"name": "Mika Wikipedia Fallback", "type": "legacy"})
    draft["connections"]["Mika Wikipedia Fallback"] = {"ai_tool": [[]]}
    state = copy.deepcopy(draft)
    events = []

    class Client:
        def request(self, method, path, body=None):
            events.append((method, path))
            if method == "PUT":
                state.update(body, versionId="new")
            elif path.endswith("/deactivate"):
                state["active"] = False
            elif path.endswith("/activate"):
                state["active"] = True
            elif method == "GET":
                return state
            return {}

    assert _repair_active_search(Client(), draft, tmp_path,
                                 tmp_path / "state.json")["status"] == "ACTIVE"
    assert state["active"] is True
    assert "Mika Wikipedia Fallback" not in {n["name"] for n in state["nodes"]}
    memory = {n["name"]: n for n in state["nodes"]}
    for name in ("Mika Simple Memory", "Serious Simple Memory"):
        assert memory[name]["parameters"]["contextWindowLength"] == 4
    assert ("POST", "/workflows/draft/deactivate") in events
    assert (tmp_path / "before-single-search-update.json").is_file()


def test_reconcile_removes_legacy_search_tool_without_draft(tmp_path, monkeypatch):
    graph = {"name": "AI", "nodes": [
        {"name": name, "parameters": {"options": {"systemMessage": "Search carefully."}}}
        for name in ("AI Agent", "Mika Serious Agent")],
        "connections": {}, "settings": {}}
    _configure_search(graph)
    current = copy.deepcopy(graph)
    current["id"] = "existing"
    current["versionId"] = "v1"
    current["active"] = True
    current["nodes"].append({"name": "Serious News Fallback", "type": "legacy"})
    current["connections"]["Serious News Fallback"] = {"ai_tool": [[]]}
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
    result = reconcile("https://n8n.example.org", key, artifact, tmp_path / "state",
                       memory_root=tmp_path / "memory")
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
                       ARTIFACT, tmp_path / "state", memory_root=tmp_path / "memory")
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
    result = reconcile("https://n8n.example.org", key_file, artifact,
                       tmp_path / "state", memory_root=tmp_path / "memory")
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
    result = reconcile("https://n8n.example.org", key_file, artifact,
                       tmp_path / "state", memory_root=tmp_path / "memory")
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
    result = reconcile("https://n8n.example.org", key_file, artifact,
                       tmp_path / "state", memory_root=tmp_path / "memory")
    assert result["status"] == "ACTIVE"


def test_tavily_prefetch_is_the_only_search_provider_and_is_bounded():
    graph = json.loads((Path(__file__).resolve().parents[1] /
        "deploy/n8n/mikachan_ai_workflow.json").read_text())
    _configure_search(graph, "test-credential")
    tools = {node["name"]: node for node in graph["nodes"]}
    assert not any("Wikipedia Fallback" in name or "News Fallback" in name
                   for name in tools)
    assert not any(node.get("type") == "@n8n/mcp-registry.tavily"
                   for node in graph["nodes"])
    search = tools["Tavily HTTP Search"]
    assert search["parameters"]["url"] == "https://api.tavily.com/search"
    assert search["parameters"]["genericAuthType"] == "httpHeaderAuth"
    assert "max_results: 3" in search["parameters"]["jsonBody"]
    assert search["credentials"]["httpHeaderAuth"]["name"] == "Mikachan Tavily Search"
    assert "slice(0, 350)" in tools["Attach Search Results"]["parameters"]["jsCode"]
    assert all(not ports.get("ai_tool") for ports in graph["connections"].values())


def test_missing_optional_tavily_credential_keeps_ai_route_available():
    graph = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    _configure_search(graph)
    nodes = {node["name"]: node for node in graph["nodes"]}
    assert "credentials" not in nodes["Tavily HTTP Search"]
    assert graph["connections"]["Search Intent"]["main"][0][0]["node"] == \
        "Attach Search Results"


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
    assert nodes["Mika Simple Memory"]["parameters"]["contextWindowLength"] == 4
    assert nodes["Serious Simple Memory"]["parameters"]["contextWindowLength"] == 4


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
    result = reconcile("https://n8n.example.org", key, artifact, tmp_path / "state",
                       memory_root=tmp_path / "memory")
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
    first = reconcile("https://n8n.example.org", key_file, artifact,
                      tmp_path / "state", memory_root=tmp_path / "memory")
    assert first["status"] == "IMPORTED_SETUP_REQUIRED"
    assert first["workflow_id"] == "draft"
    assert existing["versionId"] == "owner-v2"
    second = reconcile("https://n8n.example.org", key_file, artifact,
                       tmp_path / "state", memory_root=tmp_path / "memory")
    assert second["workflow_id"] == "draft"
