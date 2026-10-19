"""The userbot account is authorized by a refreshable QR, not a phone-code flow."""

import io
from pathlib import Path

import pytest
from telethon.errors import SessionPasswordNeededError

from mikachan.cli import _login_with_qr, _save_managed_userbot_session


class FakeQR:
    url = "tg://login?token=secret-test-token"

    def __init__(self, errors=()):
        self.errors = list(errors)
        self.refreshes = 0

    async def wait(self):
        if self.errors:
            raise self.errors.pop(0)

    async def recreate(self):
        self.refreshes += 1


class FakeClient:
    def __init__(self, qr):
        self.qr = qr
        self.connected = False
        self.password = None

    async def connect(self):
        self.connected = True

    async def qr_login(self):
        return self.qr

    async def sign_in(self, *, password):
        self.password = password


@pytest.mark.asyncio
async def test_qr_login_refreshes_expired_code_without_printing_token():
    qr = FakeQR([TimeoutError()])
    client = FakeClient(qr)
    output = io.StringIO()
    await _login_with_qr(client, output)
    assert client.connected
    assert qr.refreshes == 1
    assert "Link Desktop Device" in output.getvalue()
    assert "secret-test-token" not in output.getvalue()


@pytest.mark.asyncio
async def test_qr_login_handles_two_step_verification(monkeypatch):
    qr = FakeQR([SessionPasswordNeededError(None)])
    client = FakeClient(qr)
    monkeypatch.setattr("mikachan.cli.getpass.getpass", lambda _: "private password")
    await _login_with_qr(client, io.StringIO())
    assert client.password == "private password"


@pytest.mark.asyncio
async def test_qr_login_stops_after_five_expirations():
    qr = FakeQR([TimeoutError()] * 5)
    with pytest.raises(TimeoutError, match="five attempts"):
        await _login_with_qr(FakeClient(qr), io.StringIO())
    assert qr.refreshes == 4


def test_managed_qr_login_registers_session_through_manager(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from deploy import manager

    current = tmp_path / "current"
    (current / "deploy").mkdir(parents=True)
    (current / "deploy/manager.py").touch()
    (tmp_path / ".managed-by-mikachan").write_text("Mikachan manager schema 1\n")
    monkeypatch.chdir(current)
    monkeypatch.setattr("mikachan.cli.os.geteuid", lambda: 0)
    saved = []
    monkeypatch.setattr(manager.Manager, "set_config", lambda _self, key, value:
                        saved.append((key, value)))
    assert _save_managed_userbot_session("secret", root=tmp_path)
    assert saved == [("USERBOT_SESSION", "secret")]
