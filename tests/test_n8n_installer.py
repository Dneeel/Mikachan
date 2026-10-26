"""Offline safety and recovery checks for the independent n8n manager."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy import n8n_manager as n8n


def preflight(**changes):
    base = {"root": True, "systemd": True, "os": "ubuntu", "architecture": "x86_64",
            "cpu": 1, "memory_total": 4_000_000_000, "memory_available": 2_000_000_000,
            "swap_total": 0, "ufw": "", "nftables": "",
            "disk_free": 10_000_000_000, "inodes_free": 200_000,
            "docker": True, "compose": True, "docker_version": "28.4.0",
            "containers": [], "project_containers": [],
            "existing_n8n_containers": [], "docker_data_free": 8_000_000_000,
            "other_runtimes": {"podman": False, "containerd": False},
            "port_80": False, "port_443": False, "port_5678": False,
            "pending_apt": False, "pending_reboot": False, "mikachan_installed": True,
            "n8n_unowned": False}
    return {**base, **changes}


@pytest.fixture
def manager(tmp_path, monkeypatch):
    root = tmp_path / "n8n"
    manager = n8n.N8NManager(root)
    monkeypatch.setattr(n8n.os, "geteuid", lambda: 0)
    monkeypatch.setattr(n8n.os, "chown", lambda *_args: None)
    monkeypatch.setattr(n8n, "host_preflight", lambda _root: preflight())
    monkeypatch.setattr(manager, "_local_health", lambda: True)
    monkeypatch.setattr(manager, "_compose_owned", lambda: True)
    monkeypatch.setattr(manager, "_n8n_container_health", lambda: True)
    monkeypatch.setattr(manager, "_public_health", lambda _host: True)
    monkeypatch.setattr(manager, "_docker", lambda *_args, **_kwargs:
                        subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(manager, "_run", lambda *args, **_kwargs:
                        subprocess.CompletedProcess(args, 0, '["image@sha256:abc"]', ""))
    return manager


@pytest.mark.parametrize("bad", ["localhost", "example.local", "a..com", "-bad.com",
                                 "http://example.com", "example.com/path"])
def test_hostname_rejects_invalid_or_private_names(bad):
    with pytest.raises(n8n.N8NError):
        n8n.valid_hostname(bad)


def test_duckdns_is_a_normal_domain_and_dns_includes_cname():
    assert n8n.valid_hostname("my-automation.duckdns.org") == "my-automation.duckdns.org"
    ok, reason = n8n.check_dns("my-automation.duckdns.org", {"ipv4": "93.184.216.34", "ipv6": None},
                                {"A": ["93.184.216.34"], "AAAA": [],
                                 "CNAME": ["target.example.com"], "status": [0, 0]})
    assert ok and "matches" in reason


def test_private_tunnel_guidance_uses_current_ssh_port_and_login(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "198.51.100.2 53000 93.184.216.34 2202")
    monkeypatch.setenv("SUDO_USER", "mikachan")
    assert n8n.ssh_tunnel_command({"ipv4": "93.184.216.34", "ipv6": None}) == (
        "ssh -p 2202 -N -L 5678:127.0.0.1:5678 mikachan@93.184.216.34")


@pytest.mark.parametrize("records,expected", [
    ({"A": [], "AAAA": [], "status": [3, 3]}, "does not point"),
    ({"A": ["93.184.216.35"], "AAAA": [], "status": [0, 0]}, "Actual A"),
    ({"A": ["93.184.216.34"], "AAAA": ["2001:db8::1"], "status": [0, 0]}, "AAAA"),
])
def test_dns_mismatch_and_broken_ipv6_show_specific_fix(records, expected):
    ok, reason = n8n.check_dns("n8n.example.com",
        {"ipv4": "93.184.216.34", "ipv6": None}, records)
    assert not ok and expected in reason


def test_private_install_has_separate_root_key_and_loopback_port(manager):
    result = manager.install(access="private")
    assert result["status"] == "PRIVATE_READY"
    assert result["browser_access"] == "SSH_TUNNEL_REQUIRED"
    assert manager.owned()
    assert manager.root != Path("/opt/Mikachan")
    assert (manager.root / "secrets.env").stat().st_mode & 0o077 == 0
    assert "N8N_ENCRYPTION_KEY=" in (manager.root / "secrets.env").read_text()
    compose = manager.compose.read_text()
    assert "127.0.0.1:5678:5678" in compose
    assert "'80:80'" not in compose
    assert "postgres" not in compose.lower()
    assert manager.state()["project"] == n8n.PROJECT


def test_public_install_needs_correct_dns_and_keeps_private_raw_port(manager, monkeypatch):
    monkeypatch.setattr(n8n, "public_ips", lambda: {"ipv4": "93.184.216.34", "ipv6": None})
    monkeypatch.setattr(n8n, "dns_records", lambda _host: {
        "A": ["93.184.216.34"], "AAAA": [], "CNAME": [], "status": [0, 0]})
    result = manager.install(access="domain", hostname="n8n.example.com")
    assert result["status"] == "PUBLIC_HTTPS_READY"
    assert result["browser_access"] == "HTTPS_VERIFIED_ON_VPS_REMOTE_PENDING"
    assert result["access_url"] == "https://n8n.example.com/"
    compose = manager.compose.read_text()
    assert "127.0.0.1:5678:5678" in compose
    assert "'80:80'" in compose and "'443:443'" in compose
    env = manager.settings.read_text()
    assert "N8N_PROXY_HOPS=1" in env and "WEBHOOK_URL=https://n8n.example.com/" in env
    assert "N8N_SECURE_COOKIE=true" in env


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose CLI unavailable")
@pytest.mark.parametrize("access,hostname", [("private", ""), ("domain", "n8n.example.com")])
def test_generated_compose_is_accepted_without_starting_containers(manager, access, hostname):
    manager._configure(access, hostname, "Asia/Tehran", n8n.N8N_VERSION)
    checked = subprocess.run(["docker", "compose", "-p", n8n.PROJECT, "-f",
                              str(manager.compose), "config", "-q"],
                             capture_output=True, text=True, timeout=10, check=False)
    assert checked.returncode == 0, checked.stderr


def test_verified_registry_digest_pins_recoverable_compose_image(manager, monkeypatch):
    manager._configure("private", "", "Asia/Tehran", n8n.N8N_VERSION)
    digest = "docker.n8n.io/n8nio/n8n@sha256:" + "a" * 64
    monkeypatch.setattr(manager, "_run", lambda *_args, **_kwargs:
                        subprocess.CompletedProcess([], 0, json.dumps([digest]), ""))
    assert manager._pin_n8n_digest(n8n.N8N_VERSION) == digest
    assert f"image: {digest}" in manager.compose.read_text()


def test_access_can_change_from_private_to_verified_domain_and_back(manager, monkeypatch):
    manager.install()
    key = manager.secrets.read_bytes()
    monkeypatch.setattr(n8n, "public_ips", lambda: {"ipv4": "93.184.216.34", "ipv6": None})
    monkeypatch.setattr(n8n, "dns_records", lambda _host: {
        "A": ["93.184.216.34"], "AAAA": [], "CNAME": [], "status": [0, 0]})
    public = manager.change_access("domain", "n8n.example.com")
    assert public["status"] == "PUBLIC_HTTPS_READY"
    assert "'443:443'" in manager.compose.read_text()
    assert manager.secrets.read_bytes() == key
    assert (manager.root / "backups" / manager.state()["previous_config_snapshot"]).is_file()
    private = manager.change_access("private")
    assert private["browser_access"] == "SSH_TUNNEL_REQUIRED"
    assert "'443:443'" not in manager.compose.read_text()
    assert manager.secrets.read_bytes() == key


def test_failed_access_reconfiguration_restores_previous_compose(manager, monkeypatch):
    manager.install()
    before = manager.compose.read_bytes()
    monkeypatch.setattr(n8n, "public_ips", lambda: {"ipv4": "93.184.216.34", "ipv6": None})
    monkeypatch.setattr(n8n, "dns_records", lambda _host: {
        "A": ["93.184.216.34"], "AAAA": [], "CNAME": [], "status": [0, 0]})
    original = manager._docker
    failed = False

    def fail_first_config(*args, **kwargs):
        nonlocal failed
        if args[:2] == ("config", "-q") and not failed:
            failed = True
            raise n8n.N8NError("proxy config failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "_docker", fail_first_config)
    with pytest.raises(n8n.N8NError, match="proxy config failed"):
        manager.change_access("domain", "n8n.example.com")
    assert manager.compose.read_bytes() == before
    assert manager.state()["access"] == "private"


def test_public_certificate_failure_reports_private_only(manager, monkeypatch):
    monkeypatch.setattr(n8n, "public_ips", lambda: {"ipv4": "93.184.216.34", "ipv6": None})
    monkeypatch.setattr(n8n, "dns_records", lambda _host: {
        "A": ["93.184.216.34"], "AAAA": [], "CNAME": [], "status": [0, 0]})
    monkeypatch.setattr(manager, "_public_health", lambda _host: False)
    monkeypatch.setattr(n8n.time, "sleep", lambda _seconds: None)
    result = manager.install(access="domain", hostname="n8n.example.com")
    assert result["status"] == "PRIVATE_READY"
    assert result["access_url"] == "http://localhost:5678/"


def test_existing_public_port_owner_blocks_new_proxy_without_changes(manager, monkeypatch):
    monkeypatch.setattr(n8n, "host_preflight", lambda _root: preflight(port_443=True))
    monkeypatch.setattr(n8n, "public_ips", lambda: {"ipv4": "93.184.216.34", "ipv6": None})
    monkeypatch.setattr(n8n, "dns_records", lambda _host: {
        "A": ["93.184.216.34"], "AAAA": [], "CNAME": [], "status": [0, 0]})
    with pytest.raises(n8n.N8NError, match="Port 80 or 443"):
        manager.install(access="domain", hostname="n8n.example.com")
    assert not manager.marker.exists()


@pytest.mark.parametrize("restarts,mount_source,expected", [
    (0, "correct", True), (4, "correct", False), (0, "other", False),
])
def test_container_health_requires_stable_process_and_owned_persistence(
        tmp_path, monkeypatch, restarts, mount_source, expected):
    manager = n8n.N8NManager(tmp_path / "n8n")
    source = manager.root / "data" if mount_source == "correct" else tmp_path / "other"
    payload = [{"State": {"Running": True, "Restarting": False},
                "RestartCount": restarts,
                "Mounts": [{"Destination": "/home/node/.n8n", "Source": str(source)}]}]
    monkeypatch.setattr(manager, "_docker", lambda *_args, **_kwargs:
                        subprocess.CompletedProcess([], 0, "container-id\n", ""))
    monkeypatch.setattr(manager, "_run", lambda *_args, **_kwargs:
                        subprocess.CompletedProcess([], 0, json.dumps(payload), ""))
    assert manager._n8n_container_health() is expected


@pytest.mark.parametrize("change,phrase", [
    ({"memory_available": 100_000_000}, "memory"),
    ({"architecture": "i686"}, "ARM64"),
    ({"disk_free": 100_000_000}, "disk"),
    ({"port_5678": True}, "5678"),
    ({"existing_n8n_containers": ["other-n8n"]}, "Existing n8n"),
    ({"project_containers": ["abc"]}, "Existing n8n"),
    ({"pending_apt": True}, "package-manager"),
])
def test_unsafe_preflight_aborts_before_creating_owned_install(manager, monkeypatch, change, phrase):
    monkeypatch.setattr(n8n, "host_preflight", lambda _root: preflight(**change))
    with pytest.raises(n8n.N8NError, match=phrase):
        manager.install()
    assert not manager.marker.exists()


def test_unowned_root_is_never_adopted(manager, monkeypatch):
    manager.root.mkdir()
    (manager.root / "existing.txt").write_text("someone else")
    monkeypatch.setattr(n8n, "host_preflight", lambda _root: preflight(n8n_unowned=True))
    with pytest.raises(n8n.N8NError, match="unowned"):
        manager.install()
    assert (manager.root / "existing.txt").read_text() == "someone else"


def test_interrupted_pull_resumes_without_rotating_encryption_key(manager, monkeypatch):
    calls = []

    def docker(*args, **_kwargs):
        calls.append(args)
        if args[0] == "pull" and len([x for x in calls if x[0] == "pull"]) == 1:
            raise n8n.N8NError("Image download interrupted")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(manager, "_docker", docker)
    with pytest.raises(n8n.N8NError, match="interrupted"):
        manager.install()
    assert manager.state()["status"] == "RECOVERABLE"
    before = manager.secrets.read_bytes()
    assert manager.resume()["status"] == "PRIVATE_READY"
    assert manager.secrets.read_bytes() == before
    assert len([x for x in calls if x[0] == "pull"]) == 2


def test_missing_compose_after_interruption_is_regenerated_without_key_rotation(manager):
    manager.install()
    key = manager.secrets.read_bytes()
    manager.compose.unlink()
    assert manager.resume()["status"] == "PRIVATE_READY"
    assert manager.compose.is_file()
    assert manager.secrets.read_bytes() == key


def test_old_docker_engine_is_rejected_for_private_port_safety(manager, monkeypatch):
    monkeypatch.setattr(n8n, "host_preflight", lambda _root: preflight(docker_version="27.5.1"))
    with pytest.raises(n8n.N8NError, match="Docker Engine 28"):
        manager.install()
    assert manager.state()["status"] == "RECOVERABLE"


def test_missing_docker_never_installs_without_separate_host_consent(manager, monkeypatch):
    commands = []
    monkeypatch.setattr(manager, "_run", lambda *args, **_kwargs:
                        commands.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    with pytest.raises(n8n.N8NError, match="explicitly approve"):
        manager._install_docker(False, preflight(docker=False, compose=False,
                                                docker_version=""))
    assert commands == []


def test_docker_install_refuses_existing_active_firewall_before_apt(manager):
    with pytest.raises(n8n.N8NError, match="firewall rules"):
        manager._install_docker(True, preflight(docker=False, compose=False,
            docker_version="", ufw="Status: active"))


def test_interrupted_docker_install_can_resume_missing_compose_plugin(manager, monkeypatch):
    commands = []
    monkeypatch.setattr(manager, "_run", lambda *args, **_kwargs:
                        commands.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    partial = preflight(docker=True, compose=False, docker_version="28.4.0")
    with pytest.raises(n8n.N8NError, match="Compose v2"):
        manager._install_docker(False, partial)
    assert commands == []
    manager._install_docker(True, partial)
    assert ("apt-get", "install", "-y", "--no-install-recommends",
            "docker-compose-plugin") in commands


def test_failed_caddy_validation_never_starts_containers(manager, monkeypatch):
    monkeypatch.setattr(n8n, "public_ips", lambda: {"ipv4": "93.184.216.34", "ipv6": None})
    monkeypatch.setattr(n8n, "dns_records", lambda _host: {
        "A": ["93.184.216.34"], "AAAA": [], "CNAME": [], "status": [0, 0]})
    commands = []

    def run(*args, **_kwargs):
        commands.append(args)
        if args[:2] == ("docker", "run"):
            raise n8n.N8NError("Caddy validation failed")
        return subprocess.CompletedProcess(args, 0, '["image@sha256:abc"]', "")

    monkeypatch.setattr(manager, "_run", run)
    with pytest.raises(n8n.N8NError, match="Caddy validation"):
        manager.install(access="domain", hostname="n8n.example.com")
    assert manager.state()["status"] == "RECOVERABLE"
    assert "containers" not in manager.state()["completed"]


def test_failed_container_start_is_recoverable(manager, monkeypatch):
    calls = []

    def docker(*args, **_kwargs):
        calls.append(args)
        if args[0] == "up" and len([x for x in calls if x[0] == "up"]) == 1:
            raise n8n.N8NError("Docker startup failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(manager, "_docker", docker)
    with pytest.raises(n8n.N8NError, match="startup failed"):
        manager.install()
    assert manager.state()["status"] == "RECOVERABLE"
    assert manager.resume()["status"] == "PRIVATE_READY"


def test_simultaneous_installers_cannot_enter_same_root(manager):
    with manager.lock():
        with pytest.raises(n8n.N8NError, match="Another n8n installation"):
            with n8n.N8NManager(manager.root).lock():
                pass


def test_backup_restore_and_service_removal_preserve_data_and_key(manager):
    manager.install()
    (manager.root / "data" / "database.sqlite").write_bytes(b"original")
    key = manager.secrets.read_bytes()
    backup = manager.backup()
    assert backup.is_file() and backup.stat().st_mode & 0o077 == 0
    (manager.root / "data" / "database.sqlite").write_bytes(b"modified")
    with pytest.raises(n8n.N8NError):
        manager.restore(backup.name, "wrong")
    manager.restore(backup.name, "RESTORE N8N")
    assert (manager.root / "data" / "database.sqlite").read_bytes() == b"original"
    assert manager.secrets.read_bytes() == key
    with pytest.raises(n8n.N8NError):
        manager.remove(confirm="wrong")
    manager.remove(confirm="REMOVE N8N SERVICE")
    assert manager.secrets.read_bytes() == key
    assert (manager.root / "data" / "database.sqlite").exists()
    assert manager.state()["status"] == "SETUP_REQUIRED"


def test_permanent_n8n_removal_requires_separate_exact_confirmation(manager, monkeypatch,
                                                                     tmp_path):
    monkeypatch.setattr(n8n, "ROOT", manager.root)
    manager.wrapper = tmp_path / "n8n-manage"
    manager.install()
    (manager.root / "data" / "database.sqlite").write_bytes(b"private")
    with pytest.raises(n8n.N8NError):
        manager.remove(purge=True, confirm="REMOVE N8N SERVICE")
    assert manager.root.exists()
    manager.remove(purge=True, confirm="PURGE N8N DATA")
    assert not manager.root.exists()
    assert not manager.wrapper.exists()


def test_restore_failure_rolls_back_original_data(manager, monkeypatch):
    manager.install()
    database = manager.root / "data" / "database.sqlite"
    database.write_bytes(b"archive")
    archive = manager.backup()
    database.write_bytes(b"current")
    original = manager._docker
    up_calls = 0

    def fail_first_restore_up(*args, **kwargs):
        nonlocal up_calls
        if args[0] == "up":
            up_calls += 1
            if up_calls == 2:  # backup's restart succeeds; restored start fails
                raise n8n.N8NError("Restored container failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "_docker", fail_first_restore_up)
    with pytest.raises(n8n.N8NError, match="Restored container failed"):
        manager.restore(archive.name, "RESTORE N8N")
    assert database.read_bytes() == b"current"


def test_independent_update_preserves_key_and_mikachan_is_never_invoked(manager, monkeypatch):
    commands = []
    monkeypatch.setattr(manager, "_docker", lambda *args, **_kwargs:
                        commands.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    manager.install()
    before = manager.secrets.read_bytes()
    assert manager.update("2.44.7")["version"] == "2.44.7"
    assert manager.secrets.read_bytes() == before
    assert all("mikachan" not in " ".join(args).lower() for args in commands)


def test_failed_update_requires_database_backup_restore_before_old_image_repair(
        manager, monkeypatch):
    manager.install()
    original = manager._docker
    failed = False

    def fail_new_start(*args, **kwargs):
        nonlocal failed
        if args[0] == "up" and args[-1] == "n8n" and not failed:
            failed = True
            raise n8n.N8NError("New image failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "_docker", fail_new_start)
    with pytest.raises(n8n.N8NError, match="pre-update backup"):
        manager.update("2.44.7")
    state = manager.state()
    assert state["status"] == "RECOVERABLE"
    assert (manager.root / "backups" / state["recovery_backup"]).is_file()
    with pytest.raises(n8n.N8NError, match="migrated SQLite"):
        manager.resume()
    with pytest.raises(n8n.N8NError, match="pre-update backup"):
        manager.repair()


def test_status_and_wizard_skip_are_read_only(manager, monkeypatch, capsys):
    assert manager.status()["status"] == "NOT_INSTALLED"
    monkeypatch.setattr(n8n, "public_ips", lambda: {"ipv4": "93.184.216.34", "ipv6": None})
    monkeypatch.setattr("builtins.input", lambda _prompt: "2")
    monkeypatch.setattr(manager, "install", lambda **_kwargs: pytest.fail("install was called"))
    n8n.wizard(manager)
    assert "No, Mikachan Only" in capsys.readouterr().out
    assert not manager.root.exists()


def test_corrupt_owned_journal_reports_failed_without_starting_anything(manager):
    manager._prepare()
    manager.journal.write_text("not JSON")
    assert manager.status()["status"] == "FAILED"


def test_no_duckdns_token_prompt_and_no_unsafe_host_commands():
    source = Path(n8n.__file__).read_text()
    assert "input(\"DuckDNS token" not in source
    assert "docker system prune" not in source
    assert "docker volume prune" not in source
    assert "apt-get upgrade" not in source
    assert "dist-upgrade" not in source
    assert "reboot(" not in source


def test_journal_has_no_secret_and_cold_backup_contains_key(manager):
    manager.install()
    state = json.loads(manager.journal.read_text())
    secret = manager.secrets.read_text().split("=", 1)[1].strip()
    assert secret not in json.dumps(state)
    assert state["preflight"]["mikachan_installed"]


def test_ai_key_is_verified_before_private_save(manager, monkeypatch):
    manager.install()
    key_file = manager.root / "ai/api-key"
    monkeypatch.setattr(manager, "_reconcile_ai", lambda: {"status": "ACTIVE"})
    monkeypatch.setattr(manager, "_ensure_ai_memory", lambda: {"status": "DATABASE_READY"})

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"data":[]}'

    monkeypatch.setattr(n8n.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert manager.connect_ai("a" * 40) == {"status": "ACTIVE",
                                                "memory_status": "DATABASE_READY"}
    assert key_file.read_text().strip() == "a" * 40
    assert key_file.stat().st_mode & 0o777 == 0o600
    with pytest.raises(n8n.N8NError):
        manager.connect_ai("short")
    assert key_file.read_text().strip() == "a" * 40


def test_ai_reconcile_runs_as_module_from_mikachan_release(manager, monkeypatch,
                                                           tmp_path):
    release = tmp_path / "Mikachan" / "deploy" / "n8n"
    release.mkdir(parents=True)
    (release / "reconcile_ai.py").write_text("# test")
    (release / "mikachan_ai_workflow.json").write_text("{}")
    (manager.root / "ai").mkdir(parents=True, exist_ok=True)
    (manager.root / "ai/api-key").write_text("k" * 40)
    seen = {}

    def run(command, **kwargs):
        seen.update(command=command, cwd=kwargs["cwd"])
        return SimpleNamespace(returncode=0, stdout='{"status":"ACTIVE"}')

    monkeypatch.setattr(n8n.subprocess, "run", run)
    assert manager._reconcile_ai(release)["status"] == "ACTIVE"
    assert seen["command"][1:3] == ["-m", "deploy.n8n.reconcile_ai"]
    assert seen["cwd"] == release.parents[1]


def test_tavily_key_is_verified_and_only_saved_as_n8n_credential(manager, monkeypatch):
    manager.install()
    key_file = manager.root / "ai/api-key"
    n8n.atomic(key_file, "n" * 40 + "\n")
    monkeypatch.setattr(manager, "_reconcile_ai", lambda: {"status": "ACTIVE"})
    calls = []

    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, *_args):
            return self.body

    def urlopen(request, **_kwargs):
        calls.append(request)
        if request.full_url == "https://api.tavily.com/search":
            return Response(b'{"results":[]}')
        if request.get_method() == "GET":
            return Response(b'{"data":[]}')
        return Response(b'{"id":"cred-1"}')

    monkeypatch.setattr(n8n.urllib.request, "urlopen", urlopen)
    secret = "tvly-" + "x" * 40
    assert manager.connect_tavily(secret) == {"status": "CONNECTED",
                                             "workflow_status": "ACTIVE"}
    created = json.loads(calls[-1].data)
    assert created["type"] == "httpHeaderAuth"
    assert created["data"]["allowedDomains"] == "api.tavily.com"
    assert created["data"]["value"] == "Bearer " + secret
    assert secret not in key_file.read_text()
    assert secret not in manager.journal.read_text()


def test_automatic_memory_respects_small_vps_budget(manager, monkeypatch, tmp_path):
    release = tmp_path / "release"
    (release / "deploy/n8n").mkdir(parents=True)
    (release / "deploy/n8n/pg_memory.py").write_text("# managed helper\n")
    monkeypatch.setattr(n8n, "host_preflight", lambda _root: {
        "memory_total": 4_000_000_000, "memory_available": 300_000_000,
        "disk_free": 10_000_000_000, "inodes_free": 30_000})
    assert manager._ensure_ai_memory(release)["status"] == "RESOURCE_BLOCKED"
