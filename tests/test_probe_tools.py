"""Probe tools must be read only and avoid secret or signed URL disclosure."""

import json
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from scripts import mikachan_probe as probe


def test_readiness_only_reports_presence_and_requires_private_stage(tmp_path, monkeypatch):
    for name in ("USERBOT_SESSION", "TELEGRAM_API_HASH", "USERBOT_STAGING_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "bot.env"
    config.write_text("TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH=private_hash\n"
                      "USERBOT_SESSION=private_session\nUSERBOT_STAGING_CHAT_ID=-10012345\n")
    config.chmod(0o600)
    result = probe.readiness(config)
    assert result["large_media_route_ready_on_paper"] is True
    assert result["large_media_live_copy_verified"] is False
    assert "private_hash" not in str(result)
    assert "private_session" not in str(result)
    config.chmod(0o644)
    assert probe.readiness(config)["config_private"] is False


def test_media_probe_never_downloads_or_prints_signed_urls(monkeypatch):
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        payload = {"formats": [
            {"height": 480, "vcodec": "h264", "ext": "mp4", "protocol": "https",
             "url": "https://cdn.example/secret?signature=private"},
            {"height": 1080, "vcodec": "h264", "ext": "mp4", "protocol": "m3u8_native"},
        ]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    result = probe.media_probe(
        "https://www.pornhub.com/view_video.php?viewkey=6ac5dd5aca7cb", 30, None)
    assert result["choices_with_source_at_or_below"] == {
        "480": 480, "720": 480, "1080": 1080}
    assert result["progressive_mp4_resolutions"] == [480]
    assert "secret" not in str(result)
    assert "--skip-download" in commands[0]
    assert "--dump-single-json" in commands[0]


@pytest.mark.asyncio
async def test_follow_probe_marks_empty_listing_unverified(monkeypatch):
    async def empty_items(_target, _kind):
        return []

    async def profile(_target):
        return {"name": "Alice", "avatar": "https://private.example/avatar"}

    monkeypatch.setattr(probe, "public_items", empty_items)
    monkeypatch.setattr(probe, "public_profile", profile)
    result = await probe.follow_probe("instagram", "alice", 5)
    assert result["checks"]["posts"]["state"] == "empty_unverified"
    assert result["checks"]["profile"]["fields"] == ["avatar", "name"]
    assert "private.example" not in str(result)


def test_follow_health_reads_db_without_mutation(tmp_path):
    path = tmp_path / "state.sqlite"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE follows(id TEXT, platform TEXT, next_check REAL);
        CREATE TABLE follow_source_errors(follow_id TEXT, category TEXT, reason TEXT);
        INSERT INTO follows VALUES ('a','twitter',0);
        INSERT INTO follow_source_errors VALUES ('a','tweets','private diagnostic detail');
    """)
    db.close()
    result = probe.follow_db_health(path)
    assert result["source_errors"] == [{"platform": "twitter", "category": "tweets", "count": 1}]
    assert "private diagnostic detail" not in str(result)


def test_probe_errors_are_classified_without_echoing_provider_text():
    assert probe.error_kind(ValueError("HTTP Error 410 at signed URL secret=abc")) == \
        "source_gone_or_blocked"
    assert probe.error_kind(ValueError("Userbot transport configuration is incomplete")) == \
        "configuration_incomplete"
