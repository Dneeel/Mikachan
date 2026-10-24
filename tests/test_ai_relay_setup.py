"""Relay preparation preserves existing credentials and never prints secrets."""

import json
import os
from types import SimpleNamespace
import urllib.error

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
                                  "name": "Mikachan Private Relay"}] if FakeClient.created else []}
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


def test_webhook_verification_requires_rejected_wrong_secret(monkeypatch):
    secret = "s" * 64
    calls = []

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None

    def mocked(request, timeout):
        calls.append((request.data, timeout))
        if request.headers["X-mikachan-auth"] != secret:
            raise urllib.error.HTTPError(request.full_url, 401, "denied", {}, None)
        return Response()

    monkeypatch.setattr(relay_setup.urllib.request, "urlopen", mocked)
    assert relay_setup.verify_webhook("http://127.0.0.1:5678", secret)
    assert calls == [(b"{}", 8), (b"{}", 8)]

    monkeypatch.setattr(relay_setup.urllib.request, "urlopen",
                        lambda _request, timeout: Response())
    assert not relay_setup.verify_webhook("http://127.0.0.1:5678", secret)
