"""Independent PostgreSQL storage for Mikachan AI on an existing n8n host.

This manager never changes n8n's SQLite database or another Compose project.
It requires the managed n8n network, owns one database container, and keeps
its credentials and backups under /opt/n8n/ai-memory.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

from deploy.n8n.seed_ai_style import seed_sql


PROJECT = "mikachan_ai_memory"
NETWORK = "mikachan_n8n_default"
IMAGE = "postgres:17-alpine"


class MemoryError(RuntimeError):
    pass


def _atomic(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".memory-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class MemoryManager:
    def __init__(self, root: Path = Path("/opt/n8n/ai-memory")):
        self.root = root
        self.marker = root / ".managed-by-mikachan-ai-memory"
        self.env = root / "secrets.env"
        self.compose = root / "compose.yaml"

    def _run(self, *args: str, input_data: bytes | None = None,
             timeout: int = 60) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(args, input=input_data, capture_output=True,
                                timeout=timeout, check=False)
        if result.returncode:
            raise MemoryError(f"AI memory operation failed: {args[0]} {args[1] if len(args)>1 else ''}")
        return result

    def _docker(self, *args: str, input_data: bytes | None = None,
                timeout: int = 60) -> subprocess.CompletedProcess[bytes]:
        return self._run("docker", "compose", "--project-name", PROJECT,
                         "--file", str(self.compose), *args,
                         input_data=input_data, timeout=timeout)

    def _owned(self) -> bool:
        return self.marker.is_file() and not self.marker.is_symlink() and \
            self.marker.read_text() == "Mikachan AI memory schema 1\n"

    def status(self) -> dict[str, object]:
        if not self.root.exists():
            return {"status": "NOT_INSTALLED"}
        if not self._owned() or self.env.is_symlink() or self.compose.is_symlink():
            return {"status": "UNOWNED"}
        try:
            health = self._docker("exec", "-T", "ai-postgres", "pg_isready", "-U", "postgres",
                                  timeout=8).stdout.decode(errors="replace").strip()
            return {"status": "DATABASE_READY", "health": health,
                    "credential": "Mikachan AI Postgres",
                    "backend": (self.root / "backend").read_text().strip()
                    if (self.root / "backend").is_file() else "postgres"}
        except (MemoryError, OSError, subprocess.TimeoutExpired):
            return {"status": "RECOVERABLE"}

    def install(self, schema: Path) -> dict[str, object]:
        if self.root.exists() and not self._owned():
            raise MemoryError("Existing AI memory directory is not owned by this manager")
        if not schema.is_file() or schema.is_symlink():
            raise MemoryError("AI schema file is unavailable")
        self._run("docker", "network", "inspect", NETWORK, timeout=10)
        if not self.root.exists():
            self.root.mkdir(mode=0o700, parents=True)
            _atomic(self.marker, "Mikachan AI memory schema 1\n")
        self.root.chmod(0o700)
        (self.root / "data").mkdir(mode=0o700, exist_ok=True)
        (self.root / "backups").mkdir(mode=0o700, exist_ok=True)
        if not self.env.exists():
            _atomic(self.env, "POSTGRES_PASSWORD=" + secrets.token_urlsafe(48) + "\n" +
                    "AI_DB_PASSWORD=" + secrets.token_urlsafe(48) + "\n")
        if not (self.root / "backend").exists():
            _atomic(self.root / "backend", "postgres\n")
        if self.env.is_symlink() or self.env.stat().st_mode & 0o077:
            raise MemoryError("AI memory secret permissions are unsafe")
        init = self.root / "init/01-app.sh"
        if not init.exists():
            _atomic(init, "#!/bin/sh\nset -eu\npsql -v ON_ERROR_STOP=1 --username postgres "
                "--dbname postgres -v ai_pass=\"$AI_DB_PASSWORD\" <<'SQL'\n"
                "CREATE ROLE mika_ai LOGIN PASSWORD :'ai_pass' NOSUPERUSER NOCREATEDB NOCREATEROLE;\n"
                "CREATE DATABASE mika_ai OWNER mika_ai;\nSQL\n", 0o755)
        if not self.compose.exists():
            _atomic(self.compose, "services:\n  ai-postgres:\n    image: " + IMAGE +
                "\n    restart: unless-stopped\n    env_file: ./secrets.env\n"
                "    environment:\n      POSTGRES_USER: postgres\n      POSTGRES_DB: postgres\n"
                "    volumes:\n      - ./data:/var/lib/postgresql/data\n"
                "      - ./init/01-app.sh:/docker-entrypoint-initdb.d/01-app.sh:ro\n"
                "    networks:\n      - n8n-private\n    mem_limit: 512m\n    cpus: '0.5'\n"
                "    logging:\n      driver: json-file\n      options:\n"
                "        max-size: '10m'\n        max-file: '3'\n"
                "networks:\n  n8n-private:\n    external: true\n    name: " + NETWORK + "\n")
        self._docker("up", "-d", "--no-build", timeout=180)
        for _ in range(20):
            if self.status()["status"] == "DATABASE_READY":
                break
            time.sleep(3)
        else:
            raise MemoryError("PostgreSQL did not become ready; run status or repair")
        self._docker("exec", "-T", "ai-postgres", "psql", "-v", "ON_ERROR_STOP=1",
                     "-U", "mika_ai", "-d", "mika_ai", input_data=schema.read_bytes())
        self._docker("exec", "-T", "ai-postgres", "psql", "-v", "ON_ERROR_STOP=1",
                     "-U", "mika_ai", "-d", "mika_ai", input_data=seed_sql().encode())
        return self.status()


    def backup(self) -> Path:
        if self.status()["status"] != "DATABASE_READY":
            raise MemoryError("AI database must be healthy before backup")
        name = secrets.token_hex(8) + ".dump"
        target = self.root / "backups" / name
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            result = subprocess.run(["docker", "compose", "--project-name", PROJECT,
                "--file", str(self.compose), "exec", "-T", "ai-postgres", "pg_dump",
                "-Fc", "-U", "mika_ai", "mika_ai"], stdout=stream,
                stderr=subprocess.PIPE, timeout=120, check=False)
            stream.flush()
            os.fsync(stream.fileno())
        with target.open("rb") as stream:
            valid_dump = stream.read(5) == b"PGDMP"
        if result.returncode or not valid_dump:
            target.unlink(missing_ok=True)
            raise MemoryError("PostgreSQL backup failed or format verification failed")
        return target

    def restore(self, name: str, confirmation: str) -> dict[str, object]:
        if confirmation != "RESTORE AI MEMORY" or len(name) != 21 or \
                not name.endswith(".dump") or any(ch not in "0123456789abcdef" for ch in name[:16]):
            raise MemoryError("Restore requires an exact backup name and confirmation")
        source = self.root / "backups" / name
        if not source.is_file() or source.is_symlink():
            raise MemoryError("AI memory backup is invalid")
        with source.open("rb") as stream:
            if stream.read(5) != b"PGDMP":
                raise MemoryError("AI memory backup is invalid")
        undo = self.backup()
        try:
            with source.open("rb") as stream:
                result = subprocess.run(["docker", "compose", "--project-name", PROJECT,
                    "--file", str(self.compose), "exec", "-T", "ai-postgres", "pg_restore",
                    "--clean", "--if-exists", "--no-owner", "-U", "mika_ai", "-d", "mika_ai"],
                    stdin=stream, capture_output=True, timeout=180, check=False)
            if result.returncode:
                raise MemoryError("PostgreSQL restore failed")
        except MemoryError as exc:
            raise MemoryError(f"Restore failed; pre-restore backup preserved at {undo.name}") from exc
        return self.status()

    def select_backend(self, backend: str) -> dict[str, object]:
        if not self._owned() or backend not in {"simple", "postgres"}:
            raise MemoryError("Install managed AI memory before selecting a backend")
        _atomic(self.root / "backend", backend + "\n")
        return {"status": "BACKEND_SELECTED", "backend": backend,
                "next": "Reconcile the AI workflow to apply this selection"}

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("quick-install", "status", "repair", "backup", "restore",
                                            "simple", "postgres"))
    parser.add_argument("--schema", type=Path, default=Path(__file__).with_name("ai_schema.sql"))
    parser.add_argument("--backup")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    manager = MemoryManager()
    try:
        if args.command in {"quick-install", "repair"}:
            result: object = manager.install(args.schema)
        elif args.command == "backup":
            result = str(manager.backup())
        elif args.command == "restore":
            result = manager.restore(args.backup or "", args.confirm)
        elif args.command in {"simple", "postgres"}:
            result = manager.select_backend(args.command)
        else:
            result = manager.status()
        print(json.dumps(result))
    except (MemoryError, OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit("AI memory: " + str(exc)) from None


if __name__ == "__main__":
    main()
