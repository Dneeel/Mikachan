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
        "Telegram Trigger": {"main": [[{"node": "If", "type": "main", "index": 0}]]},
        "Switch1": {"main": [[], [], [], [{"node": "AI Agent", "type": "main", "index": 0}]]},
        "Is AI Reply?": {"main": [[{"node": "AI Agent", "type": "main", "index": 0}],
                                   [{"node": "Switch1", "type": "main", "index": 0}]]}},
        "nodes": [
            {"name": "Telegram Trigger", "type": "n8n-nodes-base.telegramTrigger"},
            {"name": "Switch1", "type": "n8n-nodes-base.switch", "parameters": {
                "rules": {"values": [{"conditions": {"conditions": [{
                    "leftValue": "old", "rightValue": "old", "operator": {"operation": "contains"}}]}}
                    for _ in range(4)]}}},
            {"name": "Get Mikasa1", "type": "n8n-nodes-base.httpRequest",
             "parameters": {"url": "https://example.invalid/old"}},
            {"name": "If", "type": "n8n-nodes-base.if", "parameters": {
                "conditions": {"conditions": [{"operator": {"operation": "exists",
                    "singleValue": True}, "rightValue": ""}]}}},
            {"name": "ElevenLabs2", "type": "elevenlabs", "parameters": {}},
            {"name": "Get Voice File1", "type": "telegram", "parameters": {
                "additionalFields": {}}},
            {"name": "Transcribe ElevenLabs1", "type": "elevenlabs"},
            {"name": "AI Agent", "type": "agent", "parameters": {"text": "old", "options": {}}},
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
    assert result["connections"]["Telegram Trigger"]["main"][0][0]["node"] == \
        "Switch1"
    rules = next(n for n in result["nodes"] if n["name"] == "Switch1")["parameters"]["rules"]["values"]
    assert "tts" in rules[1]["conditions"]["conditions"][0]["leftValue"]
    assert "میکا عکس" in rules[0]["conditions"]["conditions"][0]["leftValue"]
    assert "rating%3Ageneral" in next(n for n in result["nodes"] if
        n["name"] == "Get Mikasa1")["parameters"]["url"]
    assert result["connections"]["Switch1"]["main"][3][0]["node"] == "Analyze Message"
    assert result["connections"]["Is AI Reply?"]["main"][1][0]["node"] == "AI Agent"
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
    app.settings = SimpleNamespace(owner_telegram_id=7, mommy_ids=set())
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
                       "chat": {"id": 8, "type": "private"}, "from": {"id": 7},
                       "verified_daniel": True, "verified_owner": True,
                       "adult_verified": False,
                       "mommy_enabled": True, "mika_mode": "mommy"}


@pytest.mark.asyncio
async def test_secondary_owner_has_private_mommy_context_without_daniel_identity():
    app = App.__new__(App)
    app.store = SimpleNamespace(rate=lambda *_args: True,
                                access_registry=SimpleNamespace(role=lambda uid: "OWNER" if uid == 8 else None))
    app.n8n_gateway = SimpleNamespace(relay_telegram=AsyncMock())
    app.settings = SimpleNamespace(owner_telegram_id=7, mommy_ids=set())
    app.authorized = lambda uid, _chat, _type: uid == 8
    message = SimpleNamespace(from_user=SimpleNamespace(id=8),
        chat=SimpleNamespace(id=8, type="private"), message_id=14,
        reply_to_message=None, reply=AsyncMock())
    await app.relay_n8n_command(message, "/mika hello")
    payload = app.n8n_gateway.relay_telegram.await_args.args[0]
    assert payload["verified_owner"] is True
    assert payload["verified_daniel"] is False
    assert payload["adult_verified"] is False
    assert payload["mommy_enabled"] is True
    message.chat = SimpleNamespace(id=-100, type="supergroup")
    await app.relay_n8n_command(message, "/mika hello")
    assert app.n8n_gateway.relay_telegram.await_args.args[0]["mommy_enabled"] is False


@pytest.mark.asyncio
async def test_bare_mika_call_responds_without_empty_agent_prompt():
    app = App.__new__(App)
    app.store = SimpleNamespace(rate=lambda *_args: True, access_registry=None)
    app.n8n_gateway = SimpleNamespace(relay_telegram=AsyncMock())
    app.settings = SimpleNamespace(owner_telegram_id=7, mommy_ids=set())
    app.authorized = lambda uid, _chat, _type: uid == 7
    message = SimpleNamespace(from_user=SimpleNamespace(id=7),
        chat=SimpleNamespace(id=7, type="private"), message_id=15,
        reply_to_message=None, reply=AsyncMock())
    await app.relay_n8n_command(message, "میکا")
    message.reply.assert_awaited_once_with("اینجام دنی. چی شده؟")
    app.n8n_gateway.relay_telegram.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_iran_war_request_asks_before_ai_infers_a_conflict():
    app = App.__new__(App)
    app.store = SimpleNamespace(rate=lambda *_args: True)
    app.n8n_gateway = SimpleNamespace(relay_telegram=AsyncMock())
    app.authorized = lambda *_args: True
    message = SimpleNamespace(from_user=SimpleNamespace(id=7),
        chat=SimpleNamespace(id=7, type="private"), message_id=42,
        reply_to_message=None, reply=AsyncMock())
    await app.relay_n8n_command(message, "میکا یه چک بکن وضعیت جنگ ایرانو خلاصه بگو")
    message.reply.assert_awaited_once_with(
        "منظورت جنگ ایران و عراق است یا درگیری ایران و اسرائیل؟")
    app.n8n_gateway.relay_telegram.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_mika_prompt_reports_unconnected_relay():
    app = App.__new__(App)
    app.settings = SimpleNamespace(mika_n8n_telegram_relay=False)
    app.authorized = lambda *_args: True
    app.module_enabled = lambda *_args: True
    for text in ("میکا سلام", "can i see you"):
        message = SimpleNamespace(from_user=SimpleNamespace(id=7),
            chat=SimpleNamespace(id=7, type="private"), text=text,
            reply_to_message=None, new_chat_members=None, reply=AsyncMock())
        await app.message(message)
        message.reply.assert_awaited_once_with(
            "Mika is not connected to the owner's n8n workflow yet.")


@pytest.mark.asyncio
async def test_plain_reply_to_bot_is_marked_for_ai_row_verification():
    app = App.__new__(App)
    app.store = SimpleNamespace(rate=lambda *_args: True)
    app.n8n_gateway = SimpleNamespace(relay_telegram=AsyncMock())
    app.authorized = lambda *_args: True
    app.bot = SimpleNamespace(id=999)
    app.settings = SimpleNamespace(owner_telegram_id=7, mommy_ids=set())
    reply = SimpleNamespace(message_id=32, text="Earlier AI answer", caption=None,
                            from_user=SimpleNamespace(id=999, first_name="Mikachan"),
                            voice=None, audio=None)
    message = SimpleNamespace(from_user=SimpleNamespace(id=7),
                              chat=SimpleNamespace(id=8, type="private"),
                              message_id=33, reply_to_message=reply, reply=AsyncMock())
    await app.relay_n8n_command(message, "Please explain further")
    payload = app.n8n_gateway.relay_telegram.await_args.args[0]
    assert payload["relay_reply"] is True
    assert payload["reply_to_message"]["message_id"] == 32
