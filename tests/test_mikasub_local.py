"""Owner-managed MikaSub sources work without a Cloudflare Worker."""

import pytest

from mikachan.config import Settings
from mikachan.mikasub_core import MikaSubCore, MikaSubError
from mikachan.store import Store


@pytest.mark.asyncio
async def test_local_sources_publish_and_retain_last_good_snapshot(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite")
    settings = Settings(bot_token="123:ABC", owner_telegram_id=1,
                        allowed_telegram_ids="1", data_dir=tmp_path)
    core = MikaSubCore(settings, store.db)
    assert not core.configured
    source = "https://example.org/list.txt"
    document = b"vless://uuid@server.example:443?security=tls#Old\n"

    async def fetch(_url):
        return document

    monkeypatch.setattr("mikachan.mikasub_core.fetch_subscription", fetch)
    created = await core.add_source("Main", source)
    assert created["id"] > 0
    assert (await core.sources())[0]["name"] == "Main"
    built = await core.rebuild()
    assert built.total == 1
    info = await core.metadata()
    assert info["version"] == built.version
    items = await core.items(built.version, 0)
    assert items[0]["name"] == "🏴‍☠️ Maybe It Works 0001"
    assert "server.example:443" in items[0]["uri"]
    assert (await core.publication())["ready"]

    async def broken(_url):
        raise ValueError("temporary failure")

    monkeypatch.setattr("mikachan.mikasub_core.fetch_subscription", broken)
    with pytest.raises(MikaSubError):
        await core.rebuild()
    assert (await core.metadata())["version"] == built.version
    await core.api("PATCH", f"/api/v1/sources/{created['id']}", data={"enabled": False})
    assert not (await core.sources())[0]["enabled"]


@pytest.mark.asyncio
async def test_worker_sources_can_be_moved_to_telegram_without_deleting_worker(tmp_path,
                                                                                monkeypatch):
    store = Store(tmp_path / "state.sqlite")
    settings = Settings(bot_token="123:ABC", owner_telegram_id=1,
        allowed_telegram_ids="1", data_dir=tmp_path,
        mikasub_worker_url="https://worker.example.org",
        mikasub_admin_token="x" * 40)
    core = MikaSubCore(settings, store.db)
    assert core.configured

    async def worker_api(_method, _path):
        return {"sources": [{"name": "Owner feed", "url": "https://example.org/feed.txt",
                             "enabled": True}]}

    monkeypatch.setattr(core, "api", worker_api)
    assert await core.use_local() == 1
    assert not core.configured
    assert (await core.sources())[0]["name"] == "Owner feed"
    assert await core.use_local() == 0
    restarted = MikaSubCore(settings, store.db)
    assert not restarted.configured
    assert len(await restarted.sources()) == 1
