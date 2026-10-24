"""Cloudflare model selection never enables paid inference implicitly."""

import json
import os
from pathlib import Path

from deploy.n8n import cloudflare_ai
from deploy.n8n.reconcile_ai import _select_models


def test_prepare_discovers_single_account_and_keeps_models_disabled(tmp_path, monkeypatch):
    key = tmp_path / "api-key"
    key.write_text("k" * 40)
    os.chmod(key, 0o600)
    seen = []

    def cloudflare(path, token):
        assert token == "t" * 40
        seen.append(path)
        if path == "/user/tokens/verify":
            return {"success": True, "result": {"status": "active"}}
        if path.startswith("/accounts?"):
            return {"success": True, "result": [{"id": "a" * 32}]}
        return {"success": True, "result": [{"name": cloudflare_ai.MODEL,
            "task": {"name": "Text Generation"}}]}

    class Client:
        def __init__(self, _origin, _key):
            pass

        def request(self, method, path, body=None):
            if method == "GET":
                return {"data": []}
            assert method == "POST" and path == "/credentials"
            assert body["data"]["url"].endswith("/accounts/" + "a" * 32 + "/ai/v1")
            return {"id": "cf-1"}

    monkeypatch.setattr(cloudflare_ai, "_cloudflare", cloudflare)
    monkeypatch.setattr(cloudflare_ai, "Client", Client)
    state = tmp_path / "ai"
    result = cloudflare_ai.prepare("http://127.0.0.1:5678", key, "t" * 40, "", state)
    assert result["status"] == "CREDENTIAL_READY"
    assert len(seen) == 3
    assert not (state / "provider-selection.json").exists()
    assert "token" not in (state / "cloudflare-ai.json").read_text()

    def request(self, method, path, body=None):
        assert method == "GET"
        return {"data": [{"id": "cf-1", "type": "openAiApi",
                          "name": "Cloudflare Workers AI"}]}

    monkeypatch.setattr(Client, "request", request)
    cloudflare_ai.select("http://127.0.0.1:5678", key, state, "mika", "cloudflare")
    graph = json.loads((Path(__file__).resolve().parents[1] /
        "deploy/n8n/mikachan_ai_workflow.json").read_text())
    _select_models(graph, state)
    assert graph["connections"]["Groq Chat Model"]["ai_languageModel"] == [[]]
    assert graph["connections"]["Cloudflare Mika Model"]["ai_languageModel"][0][0]["node"] == \
        "AI Agent"
    assert graph["connections"]["Groq Serious Model"]["ai_languageModel"][0][0]["node"] == \
        "Mika Serious Agent"


def test_explicit_selection_can_adopt_unique_existing_credential(tmp_path, monkeypatch):
    key = tmp_path / "api-key"
    key.write_text("k" * 40)
    os.chmod(key, 0o600)

    class Client:
        def __init__(self, _origin, _key):
            pass

        def request(self, method, path, body=None):
            assert method == "GET" and path.startswith("/credentials?")
            return {"data": [{"id": "existing", "name": "Cloudflare Workers AI",
                              "type": "openAiApi"}]}

    monkeypatch.setattr(cloudflare_ai, "Client", Client)
    state = tmp_path / "ai"
    assert cloudflare_ai.select("http://127.0.0.1:5678", key, state, "serious",
                                "cloudflare")["status"] == "PROVIDER_SELECTED"
    marker = json.loads((state / "cloudflare-ai.json").read_text())
    assert marker["credential_id"] == "existing"
    assert marker["binding_unverified"] is True
