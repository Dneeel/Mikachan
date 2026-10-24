"""Check that the export has one private n8n entrypoint and no bot consumer."""

import httpx
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from deploy.build_n8n_relay import build
from mikachan.bot import App
from mikachan.n8n_gateway import GatewayError, N8NGateway


def test_relay_export_replaces_telegram_trigger_and_preserves_branches():
    source = {"name": "Owner export", "active": True, "connections": {
        "Telegram Trigger": {"main": [[{"node": "If", "type": "main", "index": 0}]]}},
        "nodes": [
            {"name": "Telegram Trigger", "type": "n8n-nodes-base.telegramTrigger"},
            {"name": "If", "type": "n8n-nodes-base.if", "parameters": {
                "conditions": {"conditions": [{"operator": {"operation": "exists",
                    "singleValue": True}, "rightValue": ""}]}}},
            {"name": "ElevenLabs2", "type": "elevenlabs", "parameters": {}},
            {"name": "Get Voice File1", "type": "telegram", "parameters": {
                "additionalFields": {}}},
            {"name": "Transcribe ElevenLabs1", "type": "elevenlabs"},
            {"name": "AI Agent", "type": "agent", "parameters": {"options": {}}},
        ]}
    result = build(source)
    types = [node["type"] for node in result["nodes"]]
    assert "n8n-nodes-base.telegramTrigger" not in types
    webhooks = [node for node in result["nodes"] if node["type"] ==
                "n8n-nodes-base.webhook"]
    assert len(webhooks) == 1
    assert webhooks[0]["parameters"]["authentication"] == "headerAuth"
    assert webhooks[0]["parameters"]["path"] == "mikachan-relay"
    assert result["active"] is False
    assert result["connections"]["Private Webhook"]["main"][0][0]["node"] == \
        "Telegram Trigger"
    for node in ("ElevenLabs2", "Transcribe ElevenLabs1", "AI Agent"):
        assert any(item["name"] == node for item in result["nodes"])


@pytest.mark.asyncio
async def test_private_relay_requires_secret_and_reports_missing_webhook(tmp_path):
    secret = tmp_path / "auth"
    secret.write_text("a" * 48)
    secret.chmod(0o600)
    seen = []

    def handler(request):
        seen.append((request.headers["X-Mikachan-Auth"], request.url.path,
                     request.content))
        return httpx.Response(404)

    gateway = N8NGateway(enabled=True, base_url="https://n8n.example.org",
                         webhook_path="/webhook/mikachan-relay", auth_ref=str(secret),
                         transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GatewayError, match="production webhook is not active"):
            await gateway.relay_telegram({"message_id": 3, "chat": {"id": 1}})
    finally:
        await gateway.close()
    assert seen[0][0] == "a" * 48
    assert seen[0][1] == "/webhook/mikachan-relay"
    assert b'"message_id":3' in seen[0][2]


@pytest.mark.asyncio
async def test_relay_does_not_send_unauthorized_update_or_extra_user_fields():
    app = App.__new__(App)
    app.store = SimpleNamespace(rate=lambda *_args: True)
    app.n8n_gateway = SimpleNamespace(relay_telegram=AsyncMock())
    app.authorized = lambda user, _chat, _type: user == 7
    actor = SimpleNamespace(id=8, first_name="Owner", username="private_name")
    message = SimpleNamespace(from_user=actor, chat=SimpleNamespace(id=8, type="private"),
        message_id=11, reply_to_message=None, reply=AsyncMock())
    await app.relay_n8n_command(message, "/mika hello")
    app.n8n_gateway.relay_telegram.assert_not_awaited()
    actor.id = 7
    await app.relay_n8n_command(message, "/mika hello")
    payload = app.n8n_gateway.relay_telegram.await_args.args[0]
    assert payload == {"message_id": 11, "text": "/mika hello",
                       "chat": {"id": 8}, "from": {"id": 7}, "mika_mode": "normal"}
