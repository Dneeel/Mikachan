"""Relay preparation preserves existing credentials and never prints secrets."""

import json
import os
from types import SimpleNamespace

from deploy.n8n import relay_setup


def test_fresh_relay_credential_is_created_once(tmp_path, monkeypatch):
    bot = tmp_path / "Mikachan"
    (bot / "config").mkdir(parents=True)
    (bot / "config/bot.env").write_text("configured\n")
    n8n = tmp_path / "n8n"
    n8n.mkdir()
    key = n8n / "api-key"
    key.write_text("k" * 40)
    os.chmod(key, 0o600)

    class FakeClient:
        created = None

        def __init__(self, _origin, _key):
            pass

        def request(self, method, path, body=None):
            if method == "GET":
                return {"data": [{"id": "header-1", "type": "httpHeaderAuth",
                                  "name": "Header Auth account"}] if FakeClient.created else []}
            assert method == "POST" and path == "/credentials"
            assert body["data"]["name"] == "X-Mikachan-Auth"
            FakeClient.created = body
            return {"id": "header-1"}

    monkeypatch.setattr(relay_setup, "Client", FakeClient)
    monkeypatch.setattr(relay_setup.pwd, "getpwnam",
                        lambda _name: SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid()))
    monkeypatch.setattr(relay_setup.os, "chown", lambda *_args: None)
    first = relay_setup.prepare("http://127.0.0.1:5678", key, bot_root=bot, n8n_root=n8n)
    second = relay_setup.prepare("http://127.0.0.1:5678", key, bot_root=bot, n8n_root=n8n)
    assert first["status"] == second["status"] == "WEBHOOK_CREDENTIAL_READY"
    assert "value" not in json.dumps(first)
    assert (bot / "config/n8n-relay.secret").stat().st_mode & 0o777 == 0o600
