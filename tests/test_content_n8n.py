import datetime as dt
import asyncio
import json
import os
import sqlite3
import time
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.types import Chat, Message, User

from mikachan.audio_ui import AudioUI
from mikachan.content_providers import ContentError, ContentProviders, Item
from mikachan.content_ui import ContentUI
from mikachan.n8n_gateway import GatewayError, N8NGateway, SCHEMA_VERSION
from mikachan.market_prices import MarketPrices
from mikachan.now_service import NowService
from mikachan.settings_ui import SettingsUI
from mikachan.store import Store


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, body, **kwargs):
        self.sent.append((chat, body, kwargs))
        return type("Sent", (), {"message_id": len(self.sent)})()

    async def send_photo(self, chat, url, **kwargs):
        self.sent.append((chat, url, kwargs))
        return type("Sent", (), {"message_id": len(self.sent)})()

    async def send_document(self, chat, document, **kwargs):
        self.sent.append((chat, document, kwargs))
        return type("Sent", (), {"message_id": len(self.sent)})()

    async def delete_message(self, *args, **kwargs):
        pass

    async def edit_message_text(self, body, **kwargs):
        self.sent.append((kwargs.get("chat_id"), body, kwargs))


def _db():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


@pytest.mark.asyncio
async def test_pishi_fetches_fresh_keyless_photo_without_owner_review():
    calls = 0

    def handler(request):
        nonlocal calls
        assert request.url.host == "cataas.com"
        calls += 1
        return httpx.Response(200, json={"id": f"cat{calls:03}"})

    content = ContentUI(_db(), FakeBot(), ContentProviders(httpx.MockTransport(handler)))
    state = {}
    await content._cat(state)
    assert state["item"]["identifier"] == "cat001"
    assert state["item"]["provider"] == "CATAAS"
    await content._cat(state)
    assert state["item"]["identifier"] == "cat002"
    assert calls == 2


@pytest.mark.asyncio
async def test_owner_key_uses_header_and_keyless_fallback_on_provider_failure():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "api.thecatapi.com":
            assert request.headers["x-api-key"] == "free-owner-key-123456"
            assert "free-owner-key" not in str(request.url)
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"id": "fallback123"})

    content = ContentUI(_db(), FakeBot(), ContentProviders(httpx.MockTransport(handler)))
    content.set_cat_api_key("free-owner-key-123456")
    assert content.cat_api_key() == "free-owner-key-123456"
    await content._cat({})
    assert seen == ["api.thecatapi.com", "cataas.com"]
    content.remove_cat_api_key()
    assert not content.cat_api_key()


@pytest.mark.asyncio
async def test_owner_sets_and_removes_cat_key_in_private_settings(tmp_path, monkeypatch):
    class FakeMessage:
        def __init__(self):
            self.chat = SimpleNamespace(id=1, type="private")
            self.message_id = 55
            self.edit_text = AsyncMock()
            self.answer = AsyncMock(return_value=SimpleNamespace(message_id=99))

    monkeypatch.setattr("mikachan.settings_ui.Message", FakeMessage)
    store = Store(tmp_path / "bot.sqlite")
    store.seed_access({1, 2})
    store.bootstrap_access(1)
    content = ContentUI(store.db, FakeBot())
    ui = SettingsUI(store.db, store.access_registry, NowService(store.db),
                    content_ui=content)
    store.db.execute("INSERT INTO settings_sessions(token,user_id,chat_id,message_id,page,expires) "
                     "VALUES ('test',1,1,55,'home',?)", (time.time() + 900,))
    msg = FakeMessage()
    query = SimpleNamespace(from_user=SimpleNamespace(id=1), message=msg,
                            answer=AsyncMock())
    await ui.callback(query, "z:test:sourceapis")
    assert "No owner key" in msg.edit_text.await_args.args[0]
    await ui.callback(query, "z:test:sourcekey_set:thecatapi")
    reply = SimpleNamespace(from_user=SimpleNamespace(id=1), chat=msg.chat,
        text="free-owner-key-123456", reply_to_message=SimpleNamespace(message_id=99),
        delete=AsyncMock(), answer=AsyncMock())
    assert await ui.reply(reply)
    reply.delete.assert_awaited_once()
    assert content.cat_api_key() == "free-owner-key-123456"
    assert "free-owner-key" not in reply.answer.await_args.args[0]
    await ui.callback(query, "z:test:sourcekey_remove:thecatapi")
    await ui.callback(query, "z:test:confirm")
    assert not content.cat_api_key()


@pytest.mark.asyncio
async def test_navasan_uses_owner_panel_key_without_restarting(monkeypatch):
    db = _db()
    content = ContentUI(db, FakeBot())
    prices = MarketPrices(db, navasan_key="legacy-env-key")
    seen = []
    original = httpx.AsyncClient

    def transport(request):
        seen.append(request.url.params["api_key"])
        return httpx.Response(503)

    monkeypatch.setattr("mikachan.market_prices.httpx.AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(transport),
                                                  **kwargs))
    content.set_source_api_key("navasan", "owner-key-123456")
    assert await prices.navasan("USD") is None
    assert seen == ["owner-key-123456"]


@pytest.mark.asyncio
async def test_earth_root_exact_three_and_no_inaturalist_cat_path():
    content = ContentUI(_db(), FakeBot())
    body, rows, _ = content._screen({"cmd": "/earth", "view": "home", "lang": "en"})
    assert "Earth" in body
    assert [row[0][1] for row in rows] == ["species", "nature", "done"]

    def handler(request):
        assert request.url.host == "api.thecatapi.com"
        return httpx.Response(200, json=[{"id": "abc123",
            "url": "https://cdn2.thecatapi.com/images/abc123.jpg"}])

    candidate = await ContentProviders(httpx.MockTransport(handler)).cat_candidate(
        "thecatapi", "free-owner-key-123456")
    assert candidate.provider == "TheCatAPI"


@pytest.mark.asyncio
async def test_content_session_actor_chat_message_and_ttl_binding():
    db = _db()
    content = ContentUI(db, FakeBot())
    state = {"cmd": "/wiki", "view": "home", "lang": "en"}
    db.execute("INSERT INTO content_sessions VALUES (?,?,?,?,?,?)",
               ("abc", 10, -20, 30, json.dumps(state), time.time() + 30))
    assert content._session("abc", 10, -20, 30)
    assert content._session("abc", 11, -20, 30) is None
    assert content._session("abc", 10, -21, 30) is None
    assert content._session("abc", 10, -20, 31) is None
    db.execute("UPDATE content_sessions SET expires=0")
    assert content._session("abc", 10, -20, 30) is None


@pytest.mark.asyncio
async def test_earth_ambiguous_taxa_use_real_chooser_before_observation():
    class Taxa(ContentProviders):
        async def species_candidates(self, query):
            return [{"id": 1, "name": "Vulpes vulpes", "common": "Red Fox"},
                    {"id": 2, "name": "Urocyon cinereoargenteus", "common": "Gray Fox"}]

        async def species(self, query="", taxon_id=None):
            assert taxon_id == 2
            return Item("Gray Fox", "Real observation", "https://www.inaturalist.org/observations/2",
                        "iNaturalist", identifier="2")

    content = ContentUI(_db(), FakeBot(), providers=Taxa())
    state = {"cmd": "/earth", "view": "prompt", "lang": "en", "history": []}
    await content._query(state, "fox")
    assert state["view"] == "taxa"
    await content._navigate(state, "taxon:1")
    assert state["item"]["title"] == "Gray Fox"


@pytest.mark.asyncio
async def test_real_daily_parser_with_mocked_source():
    payload = {"tfa": {"title": "Real_Article", "extract": "Published by Wikipedia",
                       "content_urls": {"desktop": {"page":
                           "https://en.wikipedia.org/wiki/Real_Article"}}}}

    def handler(request):
        assert request.url.host == "en.wikipedia.org"
        return httpx.Response(200, json=payload)

    providers = ContentProviders(httpx.MockTransport(handler))
    items = await providers.daily("featured", dt.date(2026, 10, 20))
    assert items[0].title == "Real Article"
    assert items[0].date == "2026-10-20"


@pytest.mark.asyncio
async def test_content_source_allowlist_blocks_arbitrary_host():
    provider = ContentProviders()
    with pytest.raises(ContentError, match="approved"):
        await provider.json("https://127.0.0.1/private")


@pytest.mark.asyncio
async def test_n8n_contract_auth_and_schema_validation(tmp_path):
    secret = tmp_path / "n8n.secret"
    secret.write_text("s" * 48)
    os.chmod(secret, 0o600)
    received = []

    def handler(request):
        assert request.headers["X-Mikachan-Auth"] == "s" * 48
        received.append(json.loads(request.content))
        request_id = received[-1]["request_id"]
        return httpx.Response(200, json={"schema_version": SCHEMA_VERSION,
            "request_id": request_id, "status": "succeeded",
            "result": {"kind": "catalog", "items": []}})

    gateway = N8NGateway(enabled=True, base_url="https://n8n.example.org",
        webhook_path="/webhook/mikachan", auth_ref=str(secret),
        transport=httpx.MockTransport(handler))
    await gateway.get_capabilities()
    assert received[0]["task"] == "catalog.capabilities"
    assert "s" * 48 not in json.dumps(received)
    with pytest.raises(GatewayError, match="Unsupported"):
        await gateway.submit("system.shell", {}, {}, {}, "id")
    os.chmod(secret, 0o644)
    with pytest.raises(GatewayError, match="0600"):
        await gateway.get_capabilities()
    await gateway.close()


@pytest.mark.asyncio
async def test_catalog_and_content_metadata_use_bounded_cache(tmp_path):
    calls = []
    secret = tmp_path / "auth"
    secret.write_text("s" * 48)
    os.chmod(secret, 0o600)

    def handler(request):
        calls.append(request)
        if request.url.host == "n8n.example.org":
            req = json.loads(request.content)
            return httpx.Response(200, json={"schema_version": SCHEMA_VERSION,
                "request_id": req["request_id"], "status": "succeeded",
                "result": {"kind": "catalog", "items": []}})
        return httpx.Response(200, json={"query": {"search": [],
            "searchinfo": {"totalhits": 0}}})

    transport = httpx.MockTransport(handler)
    gateway = N8NGateway(enabled=True, base_url="https://n8n.example.org",
        webhook_path="/webhook/x", auth_ref=str(secret), transport=transport)
    await gateway.get_models("tts")
    await gateway.get_models("tts")
    assert len(calls) == 1
    await gateway.close()
    provider = ContentProviders(transport)
    await provider.wiki_search("Iran")
    await provider.wiki_search("Iran")
    assert len(calls) == 2
    await provider.close()


@pytest.mark.asyncio
async def test_n8n_rejects_wrong_schema_and_non_loopback_http(tmp_path):
    with pytest.raises(GatewayError, match="HTTPS"):
        N8NGateway(enabled=True, base_url="http://n8n.example.org",
                   webhook_path="/webhook/x", auth_ref="/tmp/no")
    secret = tmp_path / "auth"
    secret.write_text("s" * 48)
    os.chmod(secret, 0o600)
    gateway = N8NGateway(enabled=True, base_url="https://n8n.example.org",
        webhook_path="/webhook/x", auth_ref=str(secret),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "schema_version": "bad", "request_id": "bad", "status": "succeeded"})))
    with pytest.raises(GatewayError, match="incompatible"):
        await gateway.get_capabilities()


def test_audio_controls_are_model_aware_and_no_provider_call_for_ui():
    gateway = N8NGateway(enabled=False)
    ui = AudioUI(_db(), FakeBot(), gateway, tts_enabled=False, stt_enabled=False)
    state = {"task": "tts", "view": "home", "actor": 42,
             "models": [{"id": "model1", "name": "Verified model", "controls": {
                 "stability": {"min": 0, "max": 1, "default": 0.55}}}],
             "model": 0, "voices": [{"id": "v1", "name": "Verified voice"}],
             "voice": 0, "language": "auto", "controls": {}, "history": []}
    ui._reset_controls(state)
    assert state["controls"] == {"stability": 0.55}
    body, keyboard = ui._screen("safe", state)
    assert "Verified voice" in body
    assert any(button.callback_data == "au:safe:continue" for row in
               keyboard.inline_keyboard for button in row)


def test_content_item_serialization_no_media_bytes():
    item = Item("Test", "Source-backed", "https://en.wikipedia.org/wiki/Test", "Wikipedia")
    assert Item(**json.loads(json.dumps(asdict(item)))) == item


@pytest.mark.asyncio
async def test_content_revocation_before_delivery_sends_nothing():
    bot = FakeBot()
    db = _db()
    content = ContentUI(db, bot, authorize=lambda actor, chat: False)
    db.execute("INSERT INTO content_sessions VALUES (?,?,?,?,?,?)",
               ("abc", 10, -20, None, json.dumps({}), time.time() + 60))
    await content._display("abc", -20, {"cmd": "/wiki", "view": "home", "lang": "en"})
    assert bot.sent == []
    assert db.execute("SELECT 1 FROM content_sessions").fetchone() is None


@pytest.mark.asyncio
async def test_content_callback_rejects_other_actor_without_source_call():
    db = _db()
    bot = FakeBot()
    content = ContentUI(db, bot)
    state = {"cmd": "/earth", "view": "home", "lang": "en", "history": []}
    db.execute("INSERT INTO content_sessions VALUES (?,?,?,?,?,?)",
               ("abc", 10, -20, 30, json.dumps(state), time.time() + 60))
    answers = []

    async def answer(value=None):
        answers.append(value)

    message = Message(message_id=30, date=dt.datetime.now(dt.UTC),
                      chat=Chat(id=-20, type="supergroup"))
    query = SimpleNamespace(from_user=User(id=11, is_bot=False, first_name="Other"),
                            message=message, answer=answer)
    assert await content.callback(query, "ct:abc:species")
    assert answers == ["This content panel has expired"]
    assert bot.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(401, "authentication"),
                                           (429, "rate limit"), (503, "unavailable")])
async def test_n8n_http_failures_are_sanitized(tmp_path, status, expected):
    secret = tmp_path / "auth"
    secret.write_text("s" * 48)
    os.chmod(secret, 0o600)
    gateway = N8NGateway(enabled=True, base_url="https://n8n.example.org",
        webhook_path="/webhook/x", auth_ref=str(secret),
        transport=httpx.MockTransport(lambda request: httpx.Response(status,
            text="PRIVATE PROVIDER ERROR")))
    with pytest.raises(GatewayError, match=expected) as error:
        await gateway.get_capabilities()
    assert "PRIVATE PROVIDER ERROR" not in str(error.value)


def test_audio_session_binding_and_expiry():
    ui = AudioUI(_db(), FakeBot(), N8NGateway(enabled=False),
                 tts_enabled=False, stt_enabled=False)
    ui.db.execute("INSERT INTO audio_sessions VALUES (?,?,?,NULL,?,?,'config',NULL,NULL)",
                  ("a", 10, -20, "{}", time.time() + 10))
    ui.db.execute("UPDATE audio_sessions SET message_id=30 WHERE token='a'")
    assert ui._session("a", 10, -20, 30)
    assert ui._session("a", 11, -20, 30) is None
    assert ui._session("a", 10, -21, 30) is None
    assert ui._session("a", 10, -20, 31) is None
    ui.db.execute("UPDATE audio_sessions SET expires=0")
    assert ui._session("a", 10, -20, 30) is None


@pytest.mark.asyncio
async def test_stt_subtitle_export_uses_only_real_timestamps():
    bot = FakeBot()
    ui = AudioUI(_db(), bot, N8NGateway(enabled=False),
                 tts_enabled=False, stt_enabled=False)
    result = {"kind": "json_transcript", "text": "Hello world",
              "words": [{"text": "Hello", "start": 1.0, "end": 1.5},
                        {"text": "world", "start": 1.5, "end": 2.0}]}
    await ui._deliver(10, result, "request", "stt", "srt", 10)
    assert len(bot.sent) == 1
    assert b"00:00:01,000 --> 00:00:02,000" in bot.sent[0][1].data
    with pytest.raises(GatewayError, match="no real word timestamps"):
        await ui._deliver(10, {"kind": "text", "text": "Hello"}, "request",
                          "stt", "vtt", 10)


def test_audio_daily_caps_reserve_atomically():
    ui = AudioUI(_db(), FakeBot(), N8NGateway(enabled=False),
                 tts_enabled=False, stt_enabled=False)
    assert ui._reserve_quota(10, -20, "tts")
    assert ui._reserve_quota(10, -20, "tts")
    assert not ui._reserve_quota(10, -20, "tts")


@pytest.mark.asyncio
async def test_audio_recovery_polls_existing_job_without_resubmit():
    class RecoverGateway:
        enabled = True

        async def get_job(self, job_id):
            assert job_id == "existing-job"
            return {"status": "succeeded", "result": {"kind": "text", "text": "Real transcript"}}

        async def cancel_job(self, job_id):
            raise AssertionError("unexpected cancellation")

        async def submit(self, *args, **kwargs):
            raise AssertionError("a recovered job must never be submitted again")

    bot = FakeBot()
    ui = AudioUI(_db(), bot, RecoverGateway(), tts_enabled=False, stt_enabled=True)
    state = {"task": "stt", "output": "plain"}
    ui.db.execute("INSERT INTO audio_sessions VALUES (?,?,?,?,?,?,?,?,?)",
        ("recovery", 10, -20, 33, json.dumps(state), time.time() + 300,
         "processing", "original-request", "existing-job"))
    await ui.recover()
    await asyncio.gather(*ui.recovery_tasks)
    assert ui.db.execute("SELECT stage FROM audio_sessions").fetchone()[0] == "done"
    assert any("Real transcript" in entry[1] for entry in bot.sent)
