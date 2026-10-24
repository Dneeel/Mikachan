"""Independent, owner-scoped n8n installer and manager for Debian/Ubuntu.

This file is copied to /opt/n8n/manager.py on installation. It deliberately
imports nothing from Mikachan and never manages the Mikachan service.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import fcntl
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator


ROOT = Path("/opt/n8n")
MARKER = "Mikachan optional n8n manager schema 1"
PROJECT = "mikachan_n8n"
N8N_VERSION = "2.44.6"  # Pinned stable upstream release, checked 2026-10-23.
CADDY_VERSION = "2.11.7"
HOSTNAME = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")


class N8NError(RuntimeError):
    """User-facing failure with no command output or secrets."""


def atomic(path: Path, body: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".n8n-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def valid_hostname(value: str) -> str:
    name = value.strip().lower().rstrip(".")
    if not HOSTNAME.fullmatch(name) or name.endswith((".local", ".localhost", ".internal")):
        raise N8NError("Enter a public hostname such as n8n.example.com")
    return name


def _url_text(url: str, timeout: float = 5) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mikachan-n8n-installer/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if int(response.headers.get("Content-Length", "0")) > 262_144:
            raise N8NError("Network response was too large")
        return response.read(262_145).decode("utf-8", errors="replace")[:262_144]


def public_ips() -> dict[str, str | None]:
    """Query independent public echo endpoints; never infer a public IP from LAN routes."""
    result: dict[str, str | None] = {"ipv4": None, "ipv6": None}
    for family, url in (("ipv4", "https://api.ipify.org"),
                        ("ipv6", "https://api6.ipify.org")):
        try:
            ip = ipaddress.ip_address(_url_text(url).strip())
            if ip.is_global and (family == "ipv4") == (ip.version == 4):
                result[family] = str(ip)
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return result


def dns_records(hostname: str) -> dict[str, Any]:
    """Read public DNS, including CNAME chain, without needing a DNS provider token."""
    name = valid_hostname(hostname)
    result: dict[str, Any] = {"A": [], "AAAA": [], "CNAME": [], "status": []}
    for kind in ("A", "AAAA"):
        url = "https://dns.google/resolve?" + urllib.parse.urlencode({"name": name, "type": kind})
        try:
            payload = json.loads(_url_text(url))
            result["status"].append(int(payload.get("Status", -1)))
            for answer in payload.get("Answer", []):
                record_type, data = answer.get("type"), str(answer.get("data", ""))
                if record_type == 5:
                    result["CNAME"].append(data.rstrip(".").lower())
                elif record_type in {1, 28}:
                    try:
                        ip = ipaddress.ip_address(data)
                        if (record_type == 1 and ip.version == 4 or
                                record_type == 28 and ip.version == 6):
                            result["A" if record_type == 1 else "AAAA"].append(str(ip))
                    except ValueError:
                        continue
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise N8NError("Public DNS could not be checked; retry or use private access") from exc
    for key in ("A", "AAAA", "CNAME"):
        result[key] = sorted(set(result[key]))
    return result


def check_dns(hostname: str, ips: dict[str, str | None],
              records: dict[str, Any]) -> tuple[bool, str]:
    expected = ips.get("ipv4")
    actual = records.get("A") or []
    ipv6 = records.get("AAAA") or []
    if not expected:
        return False, "The server's public IPv4 could not be verified. Use private access."
    if not actual or set(actual) != {expected}:
        return False, ("Your domain does not point to this server yet.\n"
                       f"Expected A: {expected}\nActual A: {', '.join(actual) or 'none'}\n"
                       f"Set the A record for {hostname} to {expected}; wait for DNS propagation.")
    if ipv6 and (not ips.get("ipv6") or set(ipv6) != {ips["ipv6"]}):
        return False, ("Your domain has an AAAA record that does not match this server.\n"
                       f"Expected AAAA: {ips.get('ipv6') or 'none'}\n"
                       f"Actual AAAA: {', '.join(ipv6)}\n"
                       "Correct/remove the AAAA record before public HTTPS setup.")
    if records.get("status") and any(code != 0 for code in records["status"]):
        return False, "DNS is not fully published yet (NXDOMAIN or propagation delay)."
    return True, ("DNS destination matches. Inbound IPv6 and remote reachability are "
                  "verified again after HTTPS setup when possible.")


def ssh_tunnel_command(ips: dict[str, str | None]) -> str:
    session = os.environ.get("SSH_CONNECTION", "").split()
    port = session[3] if len(session) == 4 and session[3].isdigit() else "22"
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "USER"
    address = ips.get("ipv4") or "YOUR_SERVER_IP"
    return f"ssh -p {port} -N -L 5678:127.0.0.1:5678 {user}@{address}"


def _meminfo() -> dict[str, int]:
    try:
        return {name: int(value.split()[0]) * 1024 for name, value in
                (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
                 if ":" in line)}
    except (OSError, ValueError, IndexError):
        return {}


def host_preflight(root: Path = ROOT) -> dict[str, Any]:
    """Read-only host assessment. Nothing is installed or restarted here."""
    memory = _meminfo()
    existing = root.exists()
    marker = root / ".managed-by-n8n-installer"
    try:
        managed = marker.is_file() and not marker.is_symlink() and \
            marker.read_text(errors="replace").startswith(MARKER)
    except OSError:
        managed = False
    disk = shutil.disk_usage(root if existing else root.parent)
    vfs = os.statvfs(root if existing else root.parent)
    def command(*args: str, timeout: int = 5) -> str:
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                    check=False)
            return result.stdout[:8000] if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""
    docker = shutil.which("docker")
    compose = command("docker", "compose", "version") if docker else ""
    docker_version = command("docker", "version", "--format", "{{.Server.Version}}").strip() if docker else ""
    containers = command("docker", "ps", "-a", "--format", "{{.Names}} {{.Image}}") if docker else ""
    project_containers = command("docker", "ps", "-a", "--filter",
        f"label=com.docker.compose.project={PROJECT}", "--format", "{{.ID}}") if docker else ""
    docker_root = command("docker", "info", "--format", "{{.DockerRootDir}}").strip() if docker else ""
    try:
        docker_free = shutil.disk_usage(docker_root).free if docker_root.startswith("/") else None
    except OSError:
        docker_free = None
    listeners = command("ss", "-ltnp")
    try:
        os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        os_release = ""
    os_id = re.search(r'^ID="?([a-z]+)"?$', os_release, re.M)
    pending_apt = command("pgrep", "-af", "(apt-get|dpkg|unattended-upgrade)")
    return {
        "root": os.geteuid() == 0, "systemd": Path("/run/systemd/system").is_dir(),
        "os": os_id.group(1) if os_id else "unknown", "architecture": platform.machine(),
        "cpu": os.cpu_count() or 1, "memory_total": memory.get("MemTotal", 0),
        "memory_available": memory.get("MemAvailable", 0),
        "swap_total": memory.get("SwapTotal", 0), "disk_free": disk.free,
        "inodes_free": vfs.f_favail, "docker": bool(docker), "compose": bool(compose),
        "docker_version": docker_version,
        "containers": containers.splitlines(), "listeners": listeners,
        "existing_n8n_containers": [line for line in containers.splitlines() if
                                    re.search(r"(?:^|\s)(?:[^\s]*n8n[^\s]*)", line, re.I)],
        "project_containers": project_containers.splitlines(),
        "docker_data_free": docker_free,
        "port_80": bool(re.search(r"(?:\*|\]|\d):80\s", listeners)),
        "port_443": bool(re.search(r"(?:\*|\]|\d):443\s", listeners)),
        "port_5678": bool(re.search(r"(?:\*|\]|\d):5678\s", listeners)),
        "other_runtimes": {tool: bool(shutil.which(tool)) for tool in ("podman", "containerd")},
        "ufw": command("ufw", "status")[:1000],
        "nftables": command("nft", "list", "ruleset")[:4000],
        "iptables": command("iptables-save")[:4000],
        "systemd_state": command("systemctl", "is-system-running").strip(),
        "pending_apt": bool(pending_apt),
        "pending_reboot": Path("/var/run/reboot-required").exists(),
        "ssh_session": bool(os.environ.get("SSH_CONNECTION")),
        "mikachan_installed": Path("/opt/Mikachan/manifest.json").is_file(),
        "n8n_managed": managed,
        "n8n_unowned": existing and any(item.name != ".install.lock" for item in root.iterdir())
                       and not managed,
    }


def _secretless_error(exc: Exception) -> str:
    return type(exc).__name__


class N8NManager:
    def __init__(self, root: Path = ROOT):
        self.root = root
        self.marker = root / ".managed-by-n8n-installer"
        self.journal = root / "installation.json"
        self.compose = root / "compose.yaml"
        self.secrets = root / "secrets.env"
        self.settings = root / "settings.env"
        self.wrapper = Path("/usr/local/bin/n8n-manage")

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".install.lock").open("a+") as stream:
            os.chmod(self.root / ".install.lock", 0o600)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise N8NError("Another n8n installation or management action is running") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def owned(self) -> bool:
        return (self.marker.is_file() and not self.marker.is_symlink() and
                self.marker.read_text(errors="replace").startswith(MARKER))

    def require_owned(self) -> None:
        if not self.owned() or self.root.is_symlink():
            raise N8NError("This n8n installation is not owned by this manager")

    def state(self) -> dict[str, Any]:
        if not self.journal.is_file():
            return {}
        try:
            record = json.loads(self.journal.read_text(encoding="utf-8"))
            if record.get("schema") != 1 or record.get("project") != PROJECT:
                raise ValueError("wrong journal schema")
            return record
        except (OSError, ValueError) as exc:
            raise N8NError("n8n installation journal needs manual inspection") from exc

    def _write_state(self, record: dict[str, Any]) -> None:
        record["updated_at"] = time.time()
        atomic(self.journal, json.dumps(record, indent=2, sort_keys=True) + "\n")

    def _run(self, *args: str, timeout: int = 1200, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                    check=False, cwd=self.root,
                                    env={**os.environ, "DEBIAN_FRONTEND": "noninteractive",
                                         "NEEDRESTART_MODE": "l"})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise N8NError(f"{args[0]} did not complete; retry from the journal") from exc
        if check and result.returncode:
            raise N8NError(f"{args[0]} failed (exit {result.returncode}); inspect n8n-manage logs")
        return result

    def _docker(self, *args: str, timeout: int = 1200, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self._run("docker", "compose", "-p", PROJECT, "-f", str(self.compose),
                         *args, timeout=timeout, check=check)

    def _prepare(self) -> None:
        if self.root.is_symlink() or self.root.exists() and not self.owned() and \
                any(item.name != ".install.lock" for item in self.root.iterdir()):
            raise N8NError("An unowned /opt/n8n exists; no files were overwritten")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        if not self.marker.exists():
            atomic(self.marker, MARKER + "\n" + secrets.token_hex(16) + "\n")
        for child in ("data", "backups", "proxy", "proxy/data", "proxy/config", "logs"):
            target = self.root / child
            if target.is_symlink():
                raise N8NError("An n8n managed path is a symlink")
            target.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root / "backups", 0o700)
        if os.geteuid() == 0:
            os.chown(self.root / "data", 1000, 1000)
        source = Path(__file__).resolve()
        target = self.root / "manager.py"
        if source != target:
            atomic(target, source.read_text(encoding="utf-8"), 0o700)
        launcher = ("#!/usr/bin/env sh\n# Mikachan optional n8n manager schema 1\n"
                    "exec /usr/bin/python3 /opt/n8n/manager.py \"$@\"\n")
        if self.root == ROOT:
            if self.wrapper.exists() and (self.wrapper.is_symlink() or
                    self.wrapper.read_text(errors="replace") != launcher):
                raise N8NError("Existing n8n-manage command is not owned by this installer")
            atomic(self.wrapper, launcher, 0o755)

    def _install_docker(self, consent: bool, preflight: dict[str, Any]) -> None:
        if preflight["docker"]:
            version = preflight.get("docker_version", "")
            match = re.match(r"^(\d+)\.", version)
            if not match or int(match[1]) < 28:
                raise N8NError("Docker Engine 28+ is required for safe localhost-only port publishing")
            if not preflight["compose"]:
                if not consent:
                    raise N8NError("Docker exists without Compose v2; install the Compose plugin separately")
                if preflight["pending_apt"]:
                    raise N8NError("A package-manager operation is already running")
                plan = self._run("apt-get", "-s", "install", "--no-install-recommends",
                                 "docker-compose-plugin", timeout=30).stdout
                if any(line.startswith("Remv ") for line in plan.splitlines()):
                    raise N8NError("Compose plugin would remove host packages")
                self._run("apt-get", "install", "-y", "--no-install-recommends",
                          "docker-compose-plugin", timeout=900)
                self._run("docker", "compose", "version", timeout=12)
            self._run("docker", "info", timeout=12)
            return
        if not consent:
            raise N8NError("Docker is missing. Review its host network effects and explicitly approve installation")
        if preflight["containers"] or preflight["other_runtimes"].get("podman") or \
                preflight["other_runtimes"].get("containerd"):
            raise N8NError("A container runtime already exists; automatic Docker installation is unsafe")
        if "Status: active" in preflight.get("ufw", "") or preflight.get("nftables"):
            raise N8NError("Existing firewall rules may be affected by Docker. "
                           "Review host networking during a maintenance window; no changes were made")
        if preflight["pending_apt"]:
            raise N8NError("A package-manager operation is already running")
        if preflight["os"] not in {"ubuntu", "debian"}:
            raise N8NError("Automatic Docker installation supports Ubuntu/Debian only")
        # Official Docker apt repository; no upgrade, package removal or global prune.
        self._run("apt-get", "update", timeout=600)
        missing_tools = [package for package, executable in
                         (("ca-certificates", "update-ca-certificates"),
                          ("curl", "curl"), ("gnupg", "gpg"))
                         if not shutil.which(executable)]
        if missing_tools:
            tools_plan = self._run("apt-get", "-s", "install", "--no-install-recommends",
                                   *missing_tools, timeout=30).stdout
            if any(line.startswith("Remv ") for line in tools_plan.splitlines()):
                raise N8NError("Required Docker tools would remove host packages")
            self._run("apt-get", "install", "-y", "--no-install-recommends",
                      *missing_tools, timeout=900)
        keyring = Path("/etc/apt/keyrings/docker.asc")
        keyring.parent.mkdir(parents=True, exist_ok=True)
        if not keyring.exists():
            key = urllib.request.urlopen(
                f"https://download.docker.com/linux/{preflight['os']}/gpg", timeout=15).read(65536)
            if b"BEGIN PGP PUBLIC KEY BLOCK" not in key:
                raise N8NError("Official Docker signing key was not received")
            atomic(keyring, key.decode("ascii"), 0o644)
        fingerprints = self._run("gpg", "--show-keys", "--with-colons", str(keyring),
                                 timeout=15).stdout
        if "9DC858229FC7DD38854AE2D88D81803C0EBFCD88" not in fingerprints:
            raise N8NError("Docker signing key fingerprint did not match the official key")
        release = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            name, _, value = line.partition("=")
            release[name] = value.strip('"')
        codename = release.get("VERSION_CODENAME", "")
        if not re.fullmatch(r"[a-z]+", codename):
            raise N8NError("Debian/Ubuntu codename is unavailable")
        repo = ("Types: deb\nURIs: https://download.docker.com/linux/" + preflight["os"] +
                "\nSuites: " + codename + "\nComponents: stable\nSigned-By: " +
                str(keyring) + "\nArchitectures: " +
                self._run("dpkg", "--print-architecture").stdout.strip() + "\n")
        source = Path("/etc/apt/sources.list.d/docker.sources")
        if source.exists() and source.read_text() != repo:
            raise N8NError("An existing Docker apt source differs; inspect it manually")
        atomic(source, repo, 0o644)
        self._run("apt-get", "update", timeout=600)
        simulation = self._run("apt-get", "-s", "install", "--no-install-recommends",
            "docker-ce", "docker-ce-cli",
            "containerd.io", "docker-buildx-plugin", "docker-compose-plugin", timeout=30).stdout
        if any(line.startswith("Remv ") for line in simulation.splitlines()):
            raise N8NError("Docker apt plan removes existing packages; manual review required")
        self._run("apt-get", "install", "-y", "--no-install-recommends",
                  "docker-ce", "docker-ce-cli",
                  "containerd.io", "docker-buildx-plugin", "docker-compose-plugin", timeout=1200)
        self._run("docker", "compose", "version", timeout=12)

    def _configure(self, access: str, hostname: str, timezone: str, version: str) -> None:
        if not VERSION.fullmatch(version):
            raise N8NError("Use a reviewed n8n version such as 2.44.6")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z][A-Za-z0-9_+-]*)+",
                            timezone) or \
                not (Path("/usr/share/zoneinfo") / timezone).is_file():
            raise N8NError("Timezone must be an installed IANA zone")
        if self.secrets.exists():
            values = self.secrets.read_text(encoding="utf-8").splitlines()
            keys = [line.split("=", 1)[1] for line in values if line.startswith("N8N_ENCRYPTION_KEY=")]
            if len(keys) != 1 or len(keys[0]) < 32:
                raise N8NError("Existing n8n encryption key is invalid; refusing rotation")
        else:
            atomic(self.secrets, "N8N_ENCRYPTION_KEY=" + secrets.token_urlsafe(48) + "\n")
        os.chmod(self.secrets, 0o600)
        public = access == "domain"
        url = f"https://{hostname}/" if public else "http://localhost:5678/"
        env = {"GENERIC_TIMEZONE": timezone,
               "TZ": timezone, "DB_TYPE": "sqlite", "N8N_HOST": hostname if public else "localhost",
               "N8N_PROTOCOL": "https" if public else "http", "N8N_PORT": "5678",
               "N8N_EDITOR_BASE_URL": url, "WEBHOOK_URL": url,
               "N8N_PROXY_HOPS": "1" if public else "0",
               "N8N_SECURE_COOKIE": "true" if public else "false",
               "EXECUTIONS_DATA_PRUNE": "true", "EXECUTIONS_DATA_MAX_AGE": "168"}
        atomic(self.settings, "".join(f"{key}={value}\n" for key, value in env.items()))
        caddy = (f"{hostname} {{\n  encode zstd gzip\n  reverse_proxy n8n:5678\n}}\n"
                 if public else "")
        atomic(self.root / "proxy/Caddyfile", caddy, 0o644)
        compose = ("name: " + PROJECT + "\nservices:\n  n8n:\n"
                   f"    image: docker.n8n.io/n8nio/n8n:{version}\n    restart: unless-stopped\n"
                   "    env_file:\n      - ./settings.env\n      - ./secrets.env\n"
                   "    ports:\n      - '127.0.0.1:5678:5678'\n"
                   "    volumes:\n      - ./data:/home/node/.n8n\n"
                   "    mem_limit: 2g\n    cpus: '1.0'\n"
                   "    logging:\n      driver: json-file\n      options:\n"
                   "        max-size: '10m'\n        max-file: '3'\n")
        if public:
            compose += (f"  caddy:\n    image: caddy:{CADDY_VERSION}\n    restart: unless-stopped\n"
                        "    ports:\n      - '80:80'\n      - '443:443'\n"
                        "    volumes:\n      - ./proxy/Caddyfile:/etc/caddy/Caddyfile:ro\n"
                        "      - ./proxy/data:/data\n      - ./proxy/config:/config\n"
                        "    mem_limit: 256m\n    cpus: '0.5'\n"
                        "    logging:\n      driver: json-file\n      options:\n"
                        "        max-size: '10m'\n        max-file: '3'\n")
        atomic(self.compose, compose, 0o600)

    def _local_health(self) -> bool:
        try:
            page = _url_text("http://127.0.0.1:5678/", timeout=5)
            return "n8n" in page.lower() or "setup" in page.lower()
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def _public_health(self, hostname: str) -> bool:
        try:
            context = ssl.create_default_context()
            request = urllib.request.Request(f"https://{hostname}/",
                                             headers={"User-Agent": "Mikachan-n8n-check/1"})
            with urllib.request.urlopen(request, timeout=8, context=context) as response:
                return response.status == 200 and b"n8n" in response.read(65536).lower()
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def _compose_owned(self) -> bool:
        try:
            result = self._run("docker", "ps", "--filter",
                f"label=com.docker.compose.project={PROJECT}", "--format",
                "{{.Label \"com.docker.compose.project\"}}", timeout=10, check=False)
            return any(line.strip() == PROJECT for line in result.stdout.splitlines())
        except N8NError:
            return False

    def _n8n_container_health(self) -> bool:
        """Verify the actual n8n container and its persistent data mount."""
        try:
            container = self._docker("ps", "-q", "n8n", timeout=10,
                                     check=False).stdout.strip()
            if not container or "\n" in container:
                return False
            details = json.loads(self._run("docker", "inspect", container,
                                          timeout=10).stdout)
            row = details[0]
            state = row["State"]
            mounts = row["Mounts"]
            return (state.get("Running") is True and state.get("Restarting") is False and
                    int(row.get("RestartCount", 0)) < 3 and
                    any(mount.get("Destination") == "/home/node/.n8n" and
                        Path(str(mount.get("Source", ""))).resolve() ==
                        (self.root / "data").resolve() for mount in mounts))
        except (N8NError, OSError, ValueError, KeyError, TypeError, IndexError):
            return False

    def _pin_n8n_digest(self, version: str) -> str:
        """Use the registry digest when Docker reports one, so backups keep the image."""
        tag = f"docker.n8n.io/n8nio/n8n:{version}"
        result = self._run("docker", "image", "inspect", tag,
                           "--format", "{{json .RepoDigests}}", timeout=20)
        try:
            digests = json.loads(result.stdout)
            digest = next(value for value in digests if isinstance(value, str) and
                re.fullmatch(r"docker\.n8n\.io/n8nio/n8n@sha256:[0-9a-f]{64}", value))
        except (ValueError, TypeError, StopIteration):
            return "unverified"
        compose = self.compose.read_text(encoding="utf-8")
        if f"image: {tag}" in compose:
            atomic(self.compose, compose.replace(f"image: {tag}", f"image: {digest}", 1))
        return digest

    def status(self) -> dict[str, Any]:
        if not self.root.exists():
            return {"status": "NOT_INSTALLED", "access_url": None}
        if not self.owned():
            return {"status": "DEGRADED", "ownership": "unowned; no action taken"}
        try:
            state = self.state()
        except N8NError:
            return {"status": "FAILED", "reason": "installation journal needs manual inspection"}
        if not state:
            return {"status": "RECOVERABLE", "stage": "prepared"}
        status = state.get("status", "RECOVERABLE")
        if status in {"PRIVATE_READY", "PUBLIC_HTTPS_READY"}:
            if not self._compose_owned() or not self._n8n_container_health() or \
                    not self._local_health():
                status = "DEGRADED"
            elif status == "PUBLIC_HTTPS_READY" and not self._public_health(str(state.get("hostname"))):
                status = "DEGRADED"
        return {"status": status, "stage": state.get("stage"),
                "access_url": state.get("access_url"), "version": state.get("version"),
                "image_digest": state.get("image_digest"), "project": PROJECT,
                "hostname": state.get("hostname"), "access": state.get("access"),
                "browser_access": ("SSH_TUNNEL_REQUIRED" if status == "PRIVATE_READY" else
                                   "HTTPS_VERIFIED_ON_VPS_REMOTE_PENDING" if
                                   status == "PUBLIC_HTTPS_READY" else "NOT_VERIFIED"),
                "remote_reachability": "not independently verified from outside this VPS",
                "updated_at": state.get("updated_at")}

    def install(self, *, access: str = "private", hostname: str = "",
                timezone: str = "Asia/Tehran", version: str = N8N_VERSION,
                allow_docker_install: bool = False, resume: bool = False) -> dict[str, Any]:
        if os.geteuid() != 0:
            raise N8NError("Run as root through sudo")
        if access not in {"domain", "private"}:
            raise N8NError("Choose a verified domain or private SSH-tunnel access")
        if access == "domain":
            hostname = valid_hostname(hostname)
        pre = host_preflight(self.root)
        if pre["n8n_unowned"] or self.root.is_symlink():
            raise N8NError("An unowned n8n installation exists; it was not modified")
        if (pre["project_containers"] or pre["existing_n8n_containers"]) and not self.owned():
            raise N8NError("Existing n8n containers or Compose project detected; no resources were adopted")
        if pre["os"] not in {"ubuntu", "debian"} or not pre["systemd"]:
            raise N8NError("A Debian/Ubuntu systemd host is required")
        if pre["architecture"] not in {"x86_64", "aarch64", "arm64"}:
            raise N8NError("The reviewed n8n/Docker image setup supports x86-64 and ARM64 hosts")
        if pre["memory_total"] < 3_000_000_000 or pre["memory_available"] < 1_200_000_000:
            raise N8NError("There isn't enough available memory to safely continue. No services were stopped")
        if pre["disk_free"] < 6_000_000_000 or pre["inodes_free"] < 100_000:
            raise N8NError("At least 6 GB free disk and 100,000 free inodes are required")
        if pre["docker_data_free"] is not None and pre["docker_data_free"] < 4_000_000_000:
            raise N8NError("Docker data root needs at least 4 GB free disk")
        if pre["pending_apt"]:
            raise N8NError("A package-manager operation is running; retry later")
        if pre["port_5678"] and not self.owned():
            raise N8NError("Port 5678 belongs to another service; nothing was changed")
        if access == "domain":
            ips = public_ips()
            ok, reason = check_dns(hostname, ips, dns_records(hostname))
            if not ok:
                raise N8NError(reason)
            if (pre["port_80"] or pre["port_443"]) and not self.owned():
                raise N8NError("Port 80 or 443 is in use. Existing proxy/services will not be interrupted")
        with self.lock():
            if not self.owned():
                # Establish ownership before writing a journal, so interruption
                # cannot leave an ambiguous unowned tree with our transaction.
                self._prepare()
            existing = self.state() if self.owned() else {}
            if existing and not resume:
                raise N8NError("A managed n8n installation exists; choose Resume, Repair or Update")
            if existing and (existing.get("access") != access or existing.get("hostname") != hostname):
                raise N8NError("Resume must use the original access method and hostname")
            record = existing or {"schema": 1, "project": PROJECT, "access": access,
                "hostname": hostname, "timezone": timezone, "version": version,
                "allow_docker_install": allow_docker_install,
                "stage": "start", "completed": [], "status": "STAGING",
                "created": [],
                "owned_resources": [str(self.root), str(self.root / "data"),
                    str(self.root / "proxy"), str(self.root / "backups"),
                    str(self.wrapper) if self.root == ROOT else str(self.root / "manager.py")],
                "compose_project": PROJECT,
                "persistent_mount": str(self.root / "data") + ":/home/node/.n8n",
                "reverse_proxy_owned": access == "domain",
                "preflight": {k: pre[k] for k in ("cpu", "memory_total", "disk_free",
                    "docker", "compose", "port_80", "port_443", "port_5678",
                    "mikachan_installed", "pending_reboot")},
                "detected_before": {"containers": pre["containers"],
                    "other_runtimes": pre["other_runtimes"],
                    "listeners": pre.get("listeners", "")[:4000]}}
            if existing and resume:
                # A crash can leave a completed journal stage but lose its last
                # atomic artifact. Recreate the stage without rotating the key.
                if not self.compose.is_file() or not self.secrets.is_file() or not self.settings.is_file():
                    record["completed"] = [name for name in record["completed"]
                                           if name not in {"configuration", "images", "proxy_validate",
                                                           "containers", "access", "health"}]
            self._write_state(record)
            def stage(name: str, action: Any) -> None:
                if name in record["completed"]:
                    return
                record["stage"] = name
                record["status"] = "STAGING"
                self._write_state(record)
                action()
                record["completed"].append(name)
                self._write_state(record)
            try:
                stage("prepared", self._prepare)
                record["created"] = [path for path in record["owned_resources"]
                                     if Path(path).exists()]
                self._write_state(record)
                stage("docker", lambda: self._install_docker(allow_docker_install, pre))
                if not pre["docker"]:
                    record["host_changes"] = ["official Docker apt packages and repository"]
                    self._write_state(record)
                stage("configuration", lambda: self._configure(access, hostname, timezone, version))
                def pull_images() -> None:
                    # One image at a time on small VPSs; a partially pulled
                    # image remains cacheable for a later Resume.
                    self._docker("pull", "--policy", "missing", "n8n", timeout=1200)
                    if access == "domain":
                        self._docker("pull", "--policy", "missing", "caddy", timeout=600)

                stage("images", pull_images)
                self._docker("config", "-q", timeout=30)
                if access == "domain":
                    stage("proxy_validate", lambda: self._run("docker", "run", "--rm",
                        "-v", f"{self.root / 'proxy/Caddyfile'}:/etc/caddy/Caddyfile:ro",
                        f"caddy:{CADDY_VERSION}", "caddy", "validate", "--config",
                        "/etc/caddy/Caddyfile", timeout=60))
                record["image_digest"] = self._pin_n8n_digest(version)
                self._write_state(record)
                self._docker("config", "-q", timeout=30)
                stage("containers", lambda: self._docker("up", "-d", "--no-build", timeout=180))
                stage("access", lambda: None)
                record["stage"] = "health"
                self._write_state(record)
                for _ in range(20):
                    if self._local_health():
                        break
                    time.sleep(3)
                if not self._local_health() or not self._compose_owned() or \
                        not self._n8n_container_health():
                    raise N8NError("n8n did not stay healthy; use View Logs then Resume/Repair")
                record["status"] = "PRIVATE_READY"
                record["access_url"] = "http://localhost:5678/"
                record["verified_health"] = {"container_running": True,
                    "persistence_attached": True, "local_editor_response": True,
                    "public_https": False, "remote_network": "pending"}
                if access == "domain":
                    for _ in range(10):
                        if self._public_health(hostname):
                            record["status"] = "PUBLIC_HTTPS_READY"
                            record["access_url"] = f"https://{hostname}/"
                            record["verified_health"]["public_https"] = True
                            break
                        time.sleep(3)
                record["stage"] = "health"
                if "health" not in record["completed"]:
                    record["completed"].append("health")
                self._write_state(record)
                result = self.status()
                result["ai_reconcile"] = self._reconcile_ai()
                return result
            except (N8NError, OSError, ValueError) as exc:
                record["status"] = "RECOVERABLE"
                record["error_type"] = _secretless_error(exc)
                self._write_state(record)
                raise

    def resume(self) -> dict[str, Any]:
        self.require_owned()
        record = self.state()
        if record.get("stage") == "update" and record.get("recovery_backup"):
            raise N8NError("An update may have migrated SQLite. Restore the recorded "
                           f"backup {record['recovery_backup']} before starting the old image")
        if not record:
            return self.install(access="private", resume=True)
        return self.install(access=record["access"], hostname=record["hostname"],
            timezone=record["timezone"], version=record["version"],
            allow_docker_install=record.get("allow_docker_install", False), resume=True)

    def repair(self) -> dict[str, Any]:
        self.require_owned()
        with self.lock():
            record = self.state()
            if record.get("stage") == "update" and record.get("recovery_backup"):
                raise N8NError("Restore the pre-update backup before repairing the old image")
            if not record:
                raise N8NError("No configured n8n installation to repair")
            record["completed"] = [stage for stage in record["completed"] if
                                   stage not in {"configuration", "images", "proxy_validate",
                                                 "containers", "access", "health"}]
            record["status"] = "RECOVERABLE"
            self._write_state(record)
        return self.resume()

    def change_access(self, access: str, hostname: str = "") -> dict[str, Any]:
        """Change only this project's browser route; preserve SQLite and the key."""
        self.require_owned()
        if access not in {"private", "domain"}:
            raise N8NError("Choose a verified domain or private SSH-tunnel access")
        hostname = valid_hostname(hostname) if access == "domain" else ""
        if access == "domain":
            ok, reason = check_dns(hostname, public_ips(), dns_records(hostname))
            if not ok:
                raise N8NError(reason)
        pre = host_preflight(self.root)
        if access == "domain" and (pre["port_80"] or pre["port_443"]) and \
                self.state().get("access") != "domain":
            raise N8NError("Port 80 or 443 is in use. Existing services will not be interrupted")
        with self.lock():
            record = self.state()
            if not record or record.get("stage") == "update":
                raise N8NError("Complete or restore the current installation before changing access")
            previous_record = json.loads(json.dumps(record))
            files = (self.settings, self.compose, self.root / "proxy/Caddyfile")
            previous = {str(path.relative_to(self.root)): path.read_text(encoding="utf-8")
                        for path in files}
            snapshot = self.root / "backups" / ("config-" +
                time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" +
                secrets.token_hex(4) + ".json")
            atomic(snapshot, json.dumps({"files": previous, "journal": record}) + "\n")
            try:
                self._configure(access, hostname, record["timezone"], record["version"])
                digest = str(record.get("image_digest", ""))
                if re.fullmatch(r"docker\.n8n\.io/n8nio/n8n@sha256:[0-9a-f]{64}", digest):
                    tag = f"docker.n8n.io/n8nio/n8n:{record['version']}"
                    atomic(self.compose, self.compose.read_text().replace(
                        f"image: {tag}", f"image: {digest}", 1))
                self._docker("config", "-q", timeout=30)
                if access == "domain":
                    self._docker("pull", "--policy", "missing", "caddy", timeout=600)
                    self._run("docker", "run", "--rm", "-v",
                              f"{self.root / 'proxy/Caddyfile'}:/etc/caddy/Caddyfile:ro",
                              f"caddy:{CADDY_VERSION}", "caddy", "validate", "--config",
                              "/etc/caddy/Caddyfile", timeout=60)
                self._docker("up", "-d", "--no-build", "--remove-orphans", timeout=180)
                if not self._local_health() or not self._n8n_container_health():
                    raise N8NError("n8n did not remain healthy after access change")
                record["access"] = access
                record["hostname"] = hostname
                record["previous_config_snapshot"] = snapshot.name
                record["status"] = "PRIVATE_READY"
                record["access_url"] = "http://localhost:5678/"
                record["verified_health"] = {"container_running": True,
                    "persistence_attached": True, "local_editor_response": True,
                    "public_https": False, "remote_network": "pending"}
                if access == "domain":
                    for _ in range(10):
                        if self._public_health(hostname):
                            record["status"] = "PUBLIC_HTTPS_READY"
                            record["access_url"] = f"https://{hostname}/"
                            record["verified_health"]["public_https"] = True
                            break
                        time.sleep(3)
                self._write_state(record)
            except (N8NError, OSError, ValueError) as exc:
                for relative, body in previous.items():
                    atomic(self.root / relative, body, 0o644 if relative.endswith("Caddyfile") else 0o600)
                try:
                    self._docker("up", "-d", "--no-build", "--remove-orphans", timeout=180)
                    self._write_state(previous_record)
                except (N8NError, OSError):
                    previous_record["status"] = "RECOVERABLE"
                    previous_record["recovery_config_snapshot"] = snapshot.name
                    self._write_state(previous_record)
                    raise N8NError("Access change and rollback need repair; previous config "
                                   f"snapshot: {snapshot.name}") from exc
                raise
        result = self.status()
        result["ai_reconcile"] = self._reconcile_ai()
        return result

    def _reconcile_ai(self) -> dict[str, str]:
        """Optional internal AI import; n8n itself remains independently usable."""
        key = self.root / "ai/api-key"
        release = Path("/opt/Mikachan/current/deploy/n8n")
        script = release / "reconcile_ai.py"
        artifact = release / "mikachan_ai_workflow.json"
        if not (key.is_file() and script.is_file() and artifact.is_file()):
            return {"status": "NOT_CONFIGURED"}
        origin = "http://127.0.0.1:5678"
        result = self._run(sys.executable, str(script), "--origin", origin,
                           "--key-file", str(key), "--artifact", str(artifact),
                           "--state-dir", str(self.root / "ai"),
                           check=False, timeout=90)
        if result.returncode:
            return {"status": "RECOVERABLE", "reason": "AI workflow reconciliation failed"}
        try:
            payload = json.loads(result.stdout)
            return payload if isinstance(payload, dict) else {"status": "RECOVERABLE"}
        except json.JSONDecodeError:
            return {"status": "RECOVERABLE", "reason": "Invalid reconciliation result"}

    def connect_ai(self, api_key: str) -> dict[str, str]:
        """Verify a pasted owner API key locally, save it privately, then reconcile."""
        self.require_owned()
        key = api_key.strip()
        if not 32 <= len(key) <= 4096 or any(ch.isspace() for ch in key):
            raise N8NError("Enter a valid n8n API key from Settings → n8n API")
        request = urllib.request.Request("http://127.0.0.1:5678/api/v1/workflows?limit=1",
            headers={"X-N8N-API-KEY": key})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.load(response)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise N8NError("The n8n API did not accept this key")
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise N8NError("n8n API key could not be verified locally; existing key was kept") from exc
        atomic(self.root / "ai/api-key", key + "\n", 0o600)
        return self._reconcile_ai()

    def service(self, action: str) -> dict[str, Any]:
        self.require_owned()
        if action not in {"start", "stop", "restart"}:
            raise N8NError("Unsupported service action")
        with self.lock():
            if action == "stop":
                self._docker("stop", timeout=120)
            else:
                self._docker("up", "-d", "--no-build", timeout=180)
                if action == "restart":
                    self._docker("restart", "n8n", timeout=120)
        return self.status()

    def logs(self, lines: int = 80) -> str:
        self.require_owned()
        raw = self._docker("logs", "--no-color", "--tail", str(min(max(lines, 1), 200)),
                           timeout=20, check=False)
        output = (raw.stdout + raw.stderr)[-20_000:]
        for secret_file in (self.secrets, self.settings):
            if secret_file.is_file():
                for line in secret_file.read_text().splitlines():
                    if "=" in line:
                        value = line.split("=", 1)[1]
                        if len(value) >= 8:
                            output = output.replace(value, "[redacted]")
        output = re.sub(r"(?i)(token|api[_-]?key|password|secret)=\S+",
                        r"\1=[redacted]", output)
        return re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", output)

    def backup(self) -> Path:
        self.require_owned()
        with self.lock():
            for folder in (self.root / "data", self.root / "proxy"):
                if folder.exists() and any(path.is_symlink() for path in folder.rglob("*")):
                    raise N8NError("n8n data contains a symlink; inspect before backup")
            source_bytes = sum(path.stat().st_size for folder in (self.root / "data",
                self.root / "proxy") if folder.exists() for path in folder.rglob("*")
                if path.is_file() and not path.is_symlink())
            if shutil.disk_usage(self.root).free < source_bytes + 1_000_000_000:
                raise N8NError("Not enough free disk for a safe n8n backup")
            was_running = self._compose_owned()
            if was_running:
                self._docker("stop", "n8n", timeout=120)
            name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4)
            target = self.root / "backups" / (name + ".tar.gz")
            try:
                with tarfile.open(target, "w:gz") as archive:
                    for relative in ("data", "secrets.env", "settings.env", "compose.yaml",
                                     "proxy/Caddyfile", "proxy/data", "proxy/config", "installation.json"):
                        path = self.root / relative
                        if path.exists() and not path.is_symlink():
                            archive.add(path, arcname=relative, recursive=True)
                os.chmod(target, 0o600)
            finally:
                if was_running:
                    self._docker("up", "-d", "--no-build", timeout=180)
            return target

    def restore(self, name: str, confirm: str) -> dict[str, Any]:
        self.require_owned()
        if confirm != "RESTORE N8N" or not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\.tar\.gz", name):
            raise N8NError("Restore requires an exact backup name and RESTORE N8N confirmation")
        archive = self.root / "backups" / name
        if not archive.is_file() or archive.is_symlink():
            raise N8NError("Backup is unavailable")
        self.backup()  # Preserve an independent undo point before replacing live data.
        with self.lock():
            staged = Path(tempfile.mkdtemp(prefix=".restore-", dir=self.root))
            rollback = Path(tempfile.mkdtemp(prefix=".restore-undo-", dir=self.root))
            names = ("data", "secrets.env", "settings.env", "compose.yaml",
                     "proxy", "installation.json")
            try:
                with tarfile.open(archive) as stream:
                    members = stream.getmembers()
                    expanded = sum(item.size for item in members)
                    if expanded > shutil.disk_usage(self.root).free // 2:
                        raise N8NError("Not enough free disk to restore this backup safely")
                    for member in members:
                        target = (staged / member.name).resolve()
                        if not target.is_relative_to(staged.resolve()) or \
                                member.name.split("/", 1)[0] not in names or \
                                not (member.isfile() or member.isdir()) or member.issym() or member.islnk():
                            raise N8NError("Backup contains an unsafe path")
                    # Members were restricted above to ordinary files/directories
                    # inside the staging root, including on Python 3.10 hosts.
                    if sys.version_info >= (3, 12):
                        stream.extractall(staged, filter="data")
                    else:
                        stream.extractall(staged)
                if not all((staged / path).exists() for path in
                           ("data", "secrets.env", "settings.env", "compose.yaml")):
                    raise N8NError("Backup is missing required n8n application files")
                self._docker("stop", timeout=120)
                moved: list[str] = []
                replaced: list[str] = []
                try:
                    for item in names:
                        current = self.root / item
                        incoming = staged / item
                        if not incoming.exists():
                            continue
                        if current.exists():
                            current.rename(rollback / item)
                            moved.append(item)
                        incoming.rename(current)
                        replaced.append(item)
                    self._docker("up", "-d", "--no-build", timeout=180)
                    if not self._local_health():
                        raise N8NError("Restored n8n did not pass its local health check")
                except (N8NError, OSError):
                    self._docker("stop", timeout=120, check=False)
                    for item in reversed(replaced):
                        current = self.root / item
                        if current.is_dir():
                            shutil.rmtree(current)
                        elif current.exists():
                            current.unlink()
                    for item in reversed(moved):
                        (rollback / item).rename(self.root / item)
                    self._docker("up", "-d", "--no-build", timeout=180)
                    raise
            finally:
                shutil.rmtree(staged, ignore_errors=True)
                shutil.rmtree(rollback, ignore_errors=True)
        return self.status()

    def update(self, version: str) -> dict[str, Any]:
        self.require_owned()
        if not VERSION.fullmatch(version):
            raise N8NError("Supply an exact reviewed n8n version")
        previous = self.state()
        snapshot = self.backup()
        with self.lock():
            activated = False
            try:
                new = dict(previous)
                new["version"] = version
                new["previous_config_snapshot"] = snapshot.name
                self._configure(new["access"], new["hostname"], new["timezone"], version)
                self._docker("pull", "n8n", timeout=1200)
                new["image_digest"] = self._pin_n8n_digest(version)
                self._docker("config", "-q", timeout=30)
                activated = True  # Compose may start a container even if it later exits nonzero.
                self._docker("up", "-d", "--no-build", "n8n", timeout=180)
                for _ in range(20):
                    if self._local_health():
                        break
                    time.sleep(3)
                if not self._local_health():
                    raise N8NError("New n8n version did not pass local health")
                self._write_state(new)
            except (N8NError, OSError) as exc:
                if not activated:
                    # A failed image pull has not touched the running database.
                    self._configure(previous["access"], previous["hostname"],
                                    previous["timezone"], previous["version"])
                else:
                    # The new image may have migrated SQLite. Never start an old
                    # image against that database. The cold pre-update backup is
                    # the explicit, recoverable rollback point.
                    self._docker("stop", "n8n", timeout=120, check=False)
                    previous["status"] = "RECOVERABLE"
                    previous["stage"] = "update"
                    previous["recovery_backup"] = snapshot.name
                    previous["update_candidate"] = version
                    self._write_state(previous)
                raise N8NError("n8n update failed; restore the pre-update backup "
                               f"{snapshot.name} if the new version modified data") from exc
        result = self.status()
        result["ai_reconcile"] = self._reconcile_ai()
        return result

    def remove(self, *, purge: bool = False, confirm: str = "") -> str:
        self.require_owned()
        if confirm != ("PURGE N8N DATA" if purge else "REMOVE N8N SERVICE"):
            raise N8NError("Type the exact removal confirmation")
        if purge and self.root != ROOT:
            raise N8NError("Purging a custom root is disabled")
        with self.lock():
            self._docker("down", timeout=180)  # No -v; never prune shared Docker resources.
            state = self.state()
            state["status"] = "SETUP_REQUIRED"
            state["stage"] = "removed"
            state["completed"] = [stage for stage in state.get("completed", []) if
                                  stage not in {"containers", "access", "health"}]
            self._write_state(state)
            if purge:
                for child in self.root.iterdir():
                    if child.is_symlink():
                        raise N8NError("Managed n8n path is a symlink; manual inspection required")
                if self.wrapper.is_file() and not self.wrapper.is_symlink() and \
                        self.wrapper.read_text(errors="replace") == (
                            "#!/usr/bin/env sh\n# Mikachan optional n8n manager schema 1\n"
                            "exec /usr/bin/python3 /opt/n8n/manager.py \"$@\"\n"):
                    self.wrapper.unlink()
                shutil.rmtree(self.root)
                return "Managed n8n service and data removed; system Docker was preserved"
            return "n8n containers removed; data, key, backups and manager preserved"


def _choose(title: str, options: list[str], *, default: int | None = None) -> int:
    print("\n" + title)
    for index, option in enumerate(options, 1):
        print(f"  {index}. {option}")
    while True:
        answer = input("Choose number (Enter = No, q = cancel): " if default == 2 and
                       len(options) == 2 else "Choose number (q to cancel): ").strip().lower()
        if not answer and default is not None:
            return default
        if answer == "q":
            raise KeyboardInterrupt
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        print("Choose one of the listed numbers.")


def managed_menu(manager: N8NManager) -> None:
    """Independent terminal controls; no Mikachan process or repository is needed."""
    labels = ["Status / access URL", "Resume installation", "Repair access / HTTPS",
              "Start", "Stop", "Restart", "View logs", "Diagnostics",
              "Backup", "Restore backup", "Update pinned n8n version",
              "Remove service (keep data)", "Uninstall service and data",
              "Change access method / domain", "Connect Mikachan AI workflow",
              "AI Memory · PostgreSQL", "Return"]
    while manager.owned():
        choice = _choose("n8n / independent management", labels)
        if choice == 17:
            return
        if choice in {12, 13}:
            phrase = "REMOVE N8N SERVICE" if choice == 12 else "PURGE N8N DATA"
            print("This affects only the managed n8n Compose project. Docker and other apps stay.")
            if choice == 13:
                print("DANGER: workflows, credentials, key, certificates and LOCAL backups will be deleted.")
            if input(f"Type {phrase} to continue: ").strip() != phrase:
                print("Cancelled. No service was removed.")
                continue
            print(manager.remove(purge=choice == 13, confirm=phrase))
            return
        result: Any
        if choice == 14:
            route = _choose("How would you like to access n8n?",
                            ["I Have a Domain", "Get Free Address", "Use Server IP"])
            if route == 3:
                result = manager.change_access("private")
            else:
                if route == 2:
                    print("Create a free DuckDNS subdomain at https://www.duckdns.org/ "
                          "and point it to this VPS. No DuckDNS token is needed.")
                print("The hostname's DNS A record must point to this server. "
                      f"Public IPv4: {public_ips().get('ipv4') or 'unavailable'}")
                result = manager.change_access("domain", input("Full hostname: "))
        elif choice == 16:
            memory_action = _choose("AI Memory · PostgreSQL",
                ["Quick Install / Repair", "Status", "Backup", "Restore",
                 "Use PostgreSQL Memory", "Use Simple Memory", "Back"])
            if memory_action == 7:
                continue
            operation = {1: "quick-install", 2: "status", 3: "backup", 4: "restore",
                         5: "postgres", 6: "simple"}[memory_action]
            release = Path("/opt/Mikachan/current")
            if not (release / "deploy/n8n/pg_memory.py").is_file():
                result = {"status": "SETUP_REQUIRED", "reason": "Mikachan AI memory code is unavailable"}
            else:
                command = [sys.executable, "-m", "deploy.n8n.pg_memory", operation]
                if operation == "restore":
                    command += ["--backup", input("Exact AI memory backup filename: ").strip(),
                                "--confirm", input("Type RESTORE AI MEMORY: ").strip()]
                process = subprocess.run(command, cwd=release, capture_output=True,
                                         text=True, timeout=300, check=False)
                result = (json.loads(process.stdout) if process.returncode == 0 else
                          {"status": "RECOVERABLE", "reason": "AI memory operation failed; "
                           "check n8n and Docker diagnostics"})
        elif choice == 15:
            print("In n8n, open Settings → n8n API, create an API key, then paste it here. "
                  "It stays in /opt/n8n/ai/api-key (mode 0600) and is never shown in logs.")
            result = manager.connect_ai(getpass.getpass("n8n API key (hidden): "))
        elif choice == 1:
            result = manager.status()
        elif choice == 2:
            result = manager.resume()
        elif choice == 3:
            result = manager.repair()
        elif choice in {4, 5, 6}:
            result = manager.service({4: "start", 5: "stop", 6: "restart"}[choice])
        elif choice == 7:
            result = manager.logs()
        elif choice == 8:
            result = {"service": manager.status(), "host": host_preflight(manager.root)}
        elif choice == 9:
            result = str(manager.backup())
        elif choice == 10:
            name = input("Exact archive filename from /opt/n8n/backups: ").strip()
            if input("Type RESTORE N8N to replace current data: ").strip() != "RESTORE N8N":
                print("Cancelled.")
                continue
            result = manager.restore(name, "RESTORE N8N")
        else:
            version = input("Exact reviewed n8n version (e.g. 2.44.6): ").strip()
            if input(f"Type UPDATE {version} to continue: ").strip() != f"UPDATE {version}":
                print("Cancelled.")
                continue
            result = manager.update(version)
        print(json.dumps(result, indent=2) if isinstance(result, dict) else result)


def wizard(manager: N8NManager) -> None:
    """Keyboard-driven optional installer; also usable after Mikachan is removed."""
    print("\nM I K A C H A N  /  OPTIONAL n8n\n")
    print("Welcome. This installs n8n separately under /opt/n8n. No Telegram setup is needed.")
    pre = host_preflight(manager.root)
    print("\nRead-only server preflight")
    print(f"CPU {pre['cpu']} · RAM {pre['memory_total']//1024**2} MiB total / "
          f"{pre['memory_available']//1024**2} MiB available · "
          f"swap {pre['swap_total']//1024**2} MiB · disk free "
          f"{pre['disk_free']//1024**3} GiB · Docker {'yes' if pre['docker'] else 'no'}")
    print(f"Other containers: {len(pre['containers'])} · 80/443 in use: "
          f"{pre['port_80']}/{pre['port_443']} · reboot pending: {pre['pending_reboot']}")
    print(f"Firewall: ufw {'present' if pre['ufw'] else 'not detected'} · "
          f"nftables {'rules found' if pre['nftables'] else 'none reported'} · "
          f"available inodes {pre['inodes_free']}")
    if pre["n8n_unowned"] or (pre["existing_n8n_containers"] and not manager.owned()):
        print("An existing n8n installation was found without this manager's ownership. "
              "It will not be adopted, overwritten or removed.")
        if _choose("Read-only options:", ["Show detected resources", "Leave unchanged"]) == 1:
            print(json.dumps({"directory": pre["n8n_unowned"],
                              "containers": pre["existing_n8n_containers"],
                              "ports": {"80": pre["port_80"], "443": pre["port_443"],
                                        "5678": pre["port_5678"]}}, indent=2))
        return
    if manager.owned():
        print("Existing managed n8n detected. Its application and data will not be overwritten.")
        managed_menu(manager)
        return
    print("\nn8n lets you build automation and AI workflows using a visual editor. It will be "
          "installed separately from Mikachan, so each application can be managed independently.")
    if _choose("Install n8n on this server?", ["Yes, Install n8n", "No, Mikachan Only"],
               default=2) == 2:
        print("Mikachan only. Nothing was changed for n8n.")
        return
    access_choice = _choose("How would you like to access n8n?",
                            ["I Have a Domain", "Get Free Address", "Use Server IP"])
    ips = public_ips()
    access, hostname = "private", ""
    if access_choice in {1, 2}:
        if access_choice == 2:
            print("A hostname makes browser access convenient, enables trusted HTTPS and "
                  "stable webhook URLs across devices. A domain is recommended, not mandatory.\n"
                  "1. Visit https://www.duckdns.org/ and sign in.\n"
                  "2. Choose an available subdomain and point it to this VPS IP: "
                  f"{ips.get('ipv4') or 'unavailable'}\n"
                  "3. Return here and enter the full hostname. No DuckDNS token is needed.\n"
                  "If the VPS IP changes, update the DuckDNS record manually.")
        else:
            print("Before continuing, make sure your domain points to this server.\n"
                  "Create/update an A record in your DNS provider using public IPv4: "
                  f"{ips.get('ipv4') or 'unavailable'}\n"
                  "If an AAAA record exists, it must match this VPS IPv6 and be reachable: "
                  f"{ips.get('ipv6') or 'unavailable'}\n"
                  "Example: n8n.example.com → your VPS IP. No domain password or DuckDNS token is needed.")
        requested: str | None = None
        while True:
            try:
                requested = valid_hostname(requested or input("Full hostname: "))
                ok, reason = check_dns(requested, ips, dns_records(requested))
            except N8NError as exc:
                ok, reason = False, str(exc)
            print(reason)
            if ok:
                hostname = requested or ""
                access = "domain"
                break
            next_choice = _choose("DNS is not ready. No services were restarted.",
                ["Retry DNS Check", "Show How to Fix", "Change Domain", "Continue with Private Access"])
            if next_choice == 2:
                print("Set the A record to", ips.get("ipv4") or "the VPS public IPv4",
                      "and correct/remove any stale AAAA. Wait for propagation.")
            elif next_choice == 3:
                requested = None
                continue
            elif next_choice == 4:
                break
    else:
        print("A trusted public IP certificate is not assumed: current bundled Caddy/ACME "
              "support is not verified for automatic IP renewal. Private SSH access is safe.\n"
              "From your own computer run: " + ssh_tunnel_command(ips) + "\n"
              "Then open http://localhost:5678/ on that same computer. On a phone, "
              "localhost means the phone; a tunnel must run on that device.")
    if access == "domain" and (pre["port_80"] or pre["port_443"]):
        print("Port 80 or 443 is already in use. Existing proxies are preserved. "
              "Choose private access or configure your own proxy separately.")
        access, hostname = "private", ""
    docker_consent = False
    if pre["docker"] and not pre["compose"]:
        print("Docker is installed, but the Compose v2 plugin is missing. "
              "The installer can add the official plugin after reviewing apt's removal plan.")
        docker_consent = _choose("Approve installing the Compose plugin?",
                                 ["Yes, install Compose plugin", "No, stop safely"]) == 1
        if not docker_consent:
            return
    elif not pre["docker"]:
        print("Docker can change host firewall/network behavior. On a fresh host only, "
              "the official Docker packages can be installed without apt upgrade or reboot.")
        docker_consent = _choose("Approve installing Docker on this host?",
                                 ["Yes, install official Docker packages", "No, stop safely"]) == 1
        if not docker_consent:
            return
    print("\nPre-installation safety summary")
    print(f"Mikachan: untouched · n8n: /opt/n8n · access: {access} "
          f"{hostname or '(SSH tunnel)'} · Docker install: {docker_consent}")
    print("No reboot, apt upgrade, firewall reset, Docker prune or unrelated service restart.")
    if _choose("Proceed with independent n8n installation?", ["Install", "Cancel"]) == 2:
        return
    result = manager.install(access=access, hostname=hostname,
                             allow_docker_install=docker_consent)
    print("\nInstallation summary\n" + json.dumps(result, indent=2))
    if result["status"] == "PUBLIC_HTTPS_READY":
        print("Open", result["access_url"], "and create the first n8n owner account.")
    elif result["status"] == "PRIVATE_READY":
        print("Private access is ready only after the SSH tunnel is running on your device. "
              "Open http://localhost:5678/ through that tunnel.")
    else:
        print("Public access is not verified. Use private SSH access and n8n-manage diagnostics.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="n8n-manage")
    commands = parser.add_subparsers(dest="command")
    for name in ("wizard", "preflight", "status", "resume", "repair", "start", "stop",
                 "restart", "logs", "diagnostics", "access", "backup"):
        commands.add_parser(name)
    install = commands.add_parser("install")
    install.add_argument("--access", choices=("private", "domain"), default="private")
    install.add_argument("--hostname", default="")
    install.add_argument("--timezone", default="Asia/Tehran")
    install.add_argument("--version", default=N8N_VERSION)
    install.add_argument("--approve-docker-install", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("backup")
    restore.add_argument("--confirm", default="")
    update = commands.add_parser("update")
    update.add_argument("version")
    access_set = commands.add_parser("access-set")
    access_set.add_argument("--method", choices=("private", "domain"), required=True)
    access_set.add_argument("--hostname", default="")
    remove = commands.add_parser("remove")
    remove.add_argument("--purge", action="store_true")
    remove.add_argument("--confirm", default="")
    commands.add_parser("ai-connect")
    args = parser.parse_args()
    manager = N8NManager()
    try:
        if args.command in {None, "wizard"}:
            if not sys.stdin.isatty():
                raise N8NError("Interactive terminal required; use an explicit subcommand")
            wizard(manager)
            return
        if args.command == "preflight":
            result: Any = host_preflight()
        elif args.command == "install":
            result = manager.install(access=args.access, hostname=args.hostname,
                timezone=args.timezone, version=args.version,
                allow_docker_install=args.approve_docker_install)
        elif args.command in {"resume", "repair"}:
            result = manager.resume() if args.command == "resume" else manager.repair()
        elif args.command in {"status", "diagnostics", "access"}:
            result = manager.status()
            if args.command == "diagnostics":
                result["preflight"] = host_preflight()
            if args.command == "access":
                result = {"status": result["status"], "access_url": result.get("access_url"),
                          "browser_access": result.get("browser_access")}
        elif args.command in {"start", "stop", "restart"}:
            result = manager.service(args.command)
        elif args.command == "logs":
            result = manager.logs()
        elif args.command == "backup":
            result = str(manager.backup())
        elif args.command == "restore":
            result = manager.restore(args.backup, args.confirm)
        elif args.command == "update":
            result = manager.update(args.version)
        elif args.command == "ai-connect":
            if not sys.stdin.isatty():
                raise N8NError("Run ai-connect in an interactive terminal to protect the key")
            result = manager.connect_ai(getpass.getpass("n8n API key (hidden): "))
        elif args.command == "access-set":
            result = manager.change_access(args.method, args.hostname)
        elif args.command == "remove":
            result = manager.remove(purge=args.purge, confirm=args.confirm)
        else:
            raise N8NError("Unsupported operation")
        print(json.dumps(result, indent=2) if isinstance(result, dict) else result)
    except KeyboardInterrupt:
        print("Cancelled; completed stages are preserved. Run n8n-manage resume to continue.")
    except (N8NError, OSError, ValueError) as exc:
        raise SystemExit("Operation failed: " + str(exc)) from exc


if __name__ == "__main__":
    main()
