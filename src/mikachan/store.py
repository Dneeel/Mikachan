"""Small transactional SQLite store. Callback decisions are compare-and-swap operations."""

import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from .access import AccessRegistry

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS confirmations (
 id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 message_id INTEGER NOT NULL, url_index INTEGER NOT NULL, prompt_id INTEGER,
 url TEXT NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
 UNIQUE(chat_id,message_id,url_index));
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 url TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
 updated REAL NOT NULL, error TEXT, artifact TEXT, receipt_id INTEGER);
CREATE TABLE IF NOT EXISTS search_sessions (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 message_id INTEGER, query TEXT NOT NULL, kinds TEXT NOT NULL, sources TEXT NOT NULL,
 page INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0,
 expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS results (
 session_id TEXT NOT NULL, generation INTEGER NOT NULL, position INTEGER NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(session_id,generation,position));
CREATE TABLE IF NOT EXISTS query_prompts (
 user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, mode TEXT NOT NULL,
 expires REAL NOT NULL, PRIMARY KEY(user_id,chat_id));
CREATE TABLE IF NOT EXISTS rate_limits (
 user_id INTEGER NOT NULL, action TEXT NOT NULL, window_start INTEGER NOT NULL,
 count INTEGER NOT NULL, PRIMARY KEY(user_id,action));
CREATE TABLE IF NOT EXISTS receipts (
 job_id TEXT NOT NULL, message_id INTEGER NOT NULL, file_id TEXT,
 PRIMARY KEY(job_id,message_id));
CREATE TABLE IF NOT EXISTS access (
 user_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS allowed_groups (chat_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS group_access_seeded (singleton INTEGER PRIMARY KEY CHECK(singleton=1));
CREATE TABLE IF NOT EXISTS admin_pending (
 id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 message_id INTEGER, target_id INTEGER NOT NULL, action TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS preferences (
 user_id INTEGER PRIMARY KEY, kinds TEXT NOT NULL, sources TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collection_selections (
 collection_id TEXT NOT NULL, item_index INTEGER NOT NULL, job_id TEXT NOT NULL,
 PRIMARY KEY(collection_id,item_index));
CREATE TABLE IF NOT EXISTS collection_batches (
 collection_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'ACTIVE');
CREATE TABLE IF NOT EXISTS collections (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 message_id INTEGER, title TEXT NOT NULL, items TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cache (
 key TEXT PRIMARY KEY, file_id TEXT NOT NULL, media_kind TEXT NOT NULL,
 owner_id INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cache_captions (
 key TEXT PRIMARY KEY, caption TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cache_audio (
 key TEXT PRIMARY KEY, has_audio INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS job_modes (
 job_id TEXT PRIMARY KEY, mode TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job_context (
 job_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collection_context (
 collection_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS derivatives (
 parent_id TEXT NOT NULL, mode TEXT NOT NULL, child_id TEXT NOT NULL,
 PRIMARY KEY(parent_id,mode));
CREATE TABLE IF NOT EXISTS job_progress (
 job_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.access_registry: AccessRegistry | None = None

    def bootstrap_access(self, primary_owner: int) -> None:
        self.access_registry = AccessRegistry(self.db, primary_owner)

    def can_access(self, user: int, chat: int, chat_type: str) -> bool:
        if self.access_registry is None:
            return False
        return self.access_registry.can_access(user, chat, chat_type)

    def rate(self, user: int, action: str, per_minute: int) -> bool:
        window = int(time.time() // 60)
        self.db.execute(
            "INSERT INTO rate_limits VALUES (?,?,?,1) ON CONFLICT(user_id,action) DO UPDATE SET window_start=excluded.window_start, count=CASE WHEN rate_limits.window_start=excluded.window_start THEN rate_limits.count+1 ELSE 1 END",
            (user, action, window),
        )
        row = self.db.execute(
            "SELECT count FROM rate_limits WHERE user_id=? AND action=?", (user, action)
        ).fetchone()
        return int(row[0]) <= per_minute

    def seed_access(self, ids: set[int]) -> None:
        if not self.db.execute("SELECT 1 FROM access LIMIT 1").fetchone():
            self.db.executemany("INSERT INTO access VALUES (?)", ((x,) for x in ids))

    def allowlist(self) -> set[int]:
        if self.access_registry is not None:
            return self.access_registry.allowlist()
        return {int(row[0]) for row in self.db.execute("SELECT user_id FROM access")}

    def seed_groups(self, ids: set[int]) -> None:
        if self.db.execute("SELECT 1 FROM group_access_seeded").fetchone():
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.executemany("INSERT OR IGNORE INTO allowed_groups VALUES (?)", ((x,) for x in ids))
            self.db.execute("INSERT INTO group_access_seeded VALUES (1)")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def groups(self) -> set[int]:
        if self.access_registry is not None:
            return self.access_registry.groups()
        return {int(row[0]) for row in self.db.execute("SELECT chat_id FROM allowed_groups")}

    def propose_access(self, owner: int, chat: int, target: int, action: str) -> str:
        user_change = action in {"add", "remove"} and 0 < target < 2**63
        group_change = action in {"group_add", "group_remove"} and -2**63 < target < 0
        if not (user_change or group_change):
            raise ValueError("invalid access change")
        token = secrets.token_urlsafe(9)
        self.db.execute(
            "INSERT INTO admin_pending VALUES (?,?,?,NULL,?,?,?)",
            (token, owner, chat, target, action, time.time() + 300),
        )
        return token

    def bind_admin_prompt(self, token: str, message_id: int) -> None:
        self.db.execute("UPDATE admin_pending SET message_id=? WHERE id=?", (message_id, token))

    def apply_access(
        self, token: str, owner: int, actual_owner: int, chat: int, message: int
    ) -> str:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT * FROM admin_pending WHERE id=? AND owner_id=? AND chat_id=? AND message_id=? AND expires>?",
                (token, owner, chat, message, time.time()),
            ).fetchone()
            if not row or owner != actual_owner:
                result = "expired or unauthorized"
            elif row["action"] == "remove" and row["target_id"] == actual_owner:
                result = "cannot remove owner"
            elif row["action"] == "add" and self.access_registry is None and len(self.allowlist()) >= 4:
                result = "maximum four users"
            else:
                if row["action"] == "add":
                    self.db.execute("INSERT OR IGNORE INTO access VALUES (?)", (row["target_id"],))
                    if self.access_registry is not None:
                        now = time.time()
                        self.db.execute("INSERT OR IGNORE INTO people VALUES "
                            "(?,?,'USER','ACTIVE',NULL,'',?,?)",
                            (row["target_id"], f"Legacy user {row['target_id']}", now, now))
                elif row["action"] == "remove":
                    self.db.execute("DELETE FROM access WHERE user_id=?", (row["target_id"],))
                    if self.access_registry is not None:
                        self.db.execute("UPDATE people SET status='REVOKED',updated=? WHERE user_id=?",
                                        (time.time(), row["target_id"]))
                    self.db.execute("UPDATE jobs SET state='CANCELLED', updated=? "
                        "WHERE user_id=? AND state IN ('QUEUED','RESOLVING','DOWNLOADING','VERIFYING')",
                        (time.time(), row["target_id"]))
                elif row["action"] == "group_add":
                    self.db.execute("INSERT OR IGNORE INTO allowed_groups VALUES (?)",
                                    (row["target_id"],))
                    if self.access_registry is not None:
                        now = time.time()
                        self.db.execute("INSERT OR IGNORE INTO group_registry VALUES "
                            "(?,?,'ACTIVE',?,?)",
                            (row["target_id"], f"Legacy group {row['target_id']}", now, now))
                else:
                    self.db.execute("DELETE FROM allowed_groups WHERE chat_id=?",
                                    (row["target_id"],))
                    if self.access_registry is not None:
                        self.db.execute("UPDATE group_registry SET status='REVOKED',updated=? "
                                        "WHERE chat_id=?", (time.time(), row["target_id"]))
                    self.db.execute("UPDATE jobs SET state='CANCELLED', updated=? "
                        "WHERE chat_id=? AND state IN ('QUEUED','RESOLVING','DOWNLOADING','VERIFYING')",
                        (time.time(), row["target_id"]))
                result = f"{row['action']} complete for {row['target_id']}"
            self.db.execute("DELETE FROM admin_pending WHERE id=?", (token,))
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def preferences(self, user: int) -> tuple[set[str], set[str]] | None:
        row = self.db.execute(
            "SELECT kinds,sources FROM preferences WHERE user_id=?", (user,)
        ).fetchone()
        return (set(json.loads(row[0])), set(json.loads(row[1]))) if row else None

    def save_preferences(self, user: int, kinds: set[str], sources: set[str]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO preferences VALUES (?,?,?)",
            (user, json.dumps(sorted(kinds)), json.dumps(sorted(sources))),
        )

    def confirm_create(
        self, chat: int, user: int, message: int, index: int, url: str, ttl: int
    ) -> str:
        cid = secrets.token_urlsafe(9)
        self.db.execute(
            "INSERT OR IGNORE INTO confirmations VALUES (?,?,?,?,?,NULL,?,?,'PENDING')",
            (cid, chat, user, message, index, url, time.time() + ttl),
        )
        row = self.db.execute(
            "SELECT id FROM confirmations WHERE chat_id=? AND message_id=? AND url_index=?",
            (chat, message, index),
        ).fetchone()
        return str(row["id"])

    def claim_prompt(self, cid: str) -> bool:
        return (
            self.db.execute(
                "UPDATE confirmations SET prompt_id=0 WHERE id=? AND prompt_id IS NULL AND state='PENDING'",
                (cid,),
            ).rowcount
            == 1
        )

    def set_prompt(self, cid: str, prompt: int) -> None:
        self.db.execute(
            "UPDATE confirmations SET prompt_id=? WHERE id=? AND prompt_id=0", (prompt, cid)
        )

    def decision(
        self, cid: str, user: int, chat: int, prompt: int, yes: bool,
        mode: str = "auto",
    ) -> tuple[str, str | None]:
        if mode not in {"auto", "audio", "360", "480", "720", "1080"}:
            raise ValueError("invalid download mode")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result: tuple[str, str | None]
            row = self.db.execute("SELECT * FROM confirmations WHERE id=?", (cid,)).fetchone()
            if (
                not row
                or row["user_id"] != user
                or row["chat_id"] != chat
                or row["prompt_id"] != prompt
            ):
                result = ("FORBIDDEN", None)
            elif row["state"] != "PENDING":
                result = ("ALREADY", None)
            else:
                state = (
                    "EXPIRED"
                    if row["expires"] <= time.time()
                    else ("ACCEPTED" if yes else "REJECTED")
                )
                self.db.execute("UPDATE confirmations SET state=? WHERE id=?", (state, cid))
                jid = None
                if state == "ACCEPTED":
                    jid = secrets.token_urlsafe(9)
                    now = time.time()
                    self.db.execute(
                        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                        (jid, user, chat, row["url"], "QUEUED", now, now, None, None),
                    )
                    self.db.execute(
                        "INSERT INTO job_progress VALUES (?,?)", (jid, row["prompt_id"])
                    )
                    if mode != "auto":
                        self.db.execute("INSERT INTO job_modes VALUES (?,?)", (jid, mode))
                result = (state, jid)
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def decision_relay(self, cid: str, user: int, chat: int,
                       prompt: int, yes: bool) -> tuple[str, str | None]:
        """Consume a confirmation for an external relay without creating a local job."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result: tuple[str, str | None]
            row = self.db.execute("SELECT * FROM confirmations WHERE id=?", (cid,)).fetchone()
            if (not row or row["user_id"] != user or row["chat_id"] != chat or
                    row["prompt_id"] != prompt):
                result = ("FORBIDDEN", None)
            elif row["state"] != "PENDING":
                result = ("ALREADY", None)
            else:
                state = "EXPIRED" if row["expires"] <= time.time() else \
                        "ACCEPTED" if yes else "REJECTED"
                self.db.execute("UPDATE confirmations SET state=? WHERE id=?", (state, cid))
                result = (state, str(row["url"]) if state == "ACCEPTED" else None)
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def create_job(
        self, user: int, chat: int, url: str,
        mode: str = "auto", context: dict[str, Any] | None = None,
    ) -> str:
        jid = secrets.token_urlsafe(9)
        now = time.time()
        self.db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,NULL)",
            (jid, user, chat, url, "QUEUED", now, now, None, None),
        )
        if mode != "auto":
            self.db.execute("INSERT INTO job_modes VALUES (?,?)", (jid, mode))
        if context:
            self.set_job_context(jid, context)
        return jid

    def job_context(self, jid: str) -> dict[str, Any]:
        row = self.db.execute("SELECT payload FROM job_context WHERE job_id=?", (jid,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_job_context(self, jid: str, context: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO job_context VALUES (?,?)", (jid, json.dumps(context))
        )

    def job_mode(self, jid: str) -> str:
        row = self.db.execute("SELECT mode FROM job_modes WHERE job_id=?", (jid,)).fetchone()
        return str(row[0]) if row else "auto"

    def progress_message(self, jid: str) -> int | None:
        row = self.db.execute(
            "SELECT message_id FROM job_progress WHERE job_id=?", (jid,)
        ).fetchone()
        return int(row[0]) if row else None

    def bind_progress(self, jid: str, message_id: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO job_progress VALUES (?,?)", (jid, message_id))

    def create_derivative(
        self, parent_id: str, user: int, chat: int, mode: str
    ) -> tuple[str | None, bool]:
        if mode != "audio":
            return None, False
        self.db.execute("BEGIN IMMEDIATE")
        try:
            parent = self.db.execute(
                "SELECT url,state FROM jobs WHERE id=? AND user_id=? AND chat_id=?",
                (parent_id, user, chat),
            ).fetchone()
            result: tuple[str | None, bool] = (None, False)
            if parent and parent["state"] == "COMPLETED":
                existing = self.db.execute(
                    "SELECT child_id FROM derivatives WHERE parent_id=? AND mode=?",
                    (parent_id, mode),
                ).fetchone()
                if existing:
                    result = (str(existing[0]), False)
                else:
                    jid = self.create_job(
                        user, chat, str(parent["url"]), mode,
                        self.job_context(parent_id),
                    )
                    self.db.execute(
                        "INSERT INTO derivatives VALUES (?,?,?)", (parent_id, mode, jid)
                    )
                    result = (jid, True)
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def create_collection(
        self, user: int, chat: int, title: str, items: list[dict[str, str]],
        context: dict[str, Any] | None = None,
    ) -> str:
        cid = secrets.token_urlsafe(9)
        self.db.execute(
            "INSERT INTO collections VALUES (?,?,?,NULL,?,?,?)",
            (cid, user, chat, title[:200], json.dumps(items), time.time() + 1800),
        )
        if context:
            self.db.execute(
                "INSERT INTO collection_context VALUES (?,?)", (cid, json.dumps(context))
            )
        return cid

    def collection_context(self, cid: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT payload FROM collection_context WHERE collection_id=?", (cid,)
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def select_collection(
        self, cid: str, user: int, chat: int, index: int, mode: str = "auto",
    ) -> tuple[str | None, bool]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result: tuple[str | None, bool]
            row = self.collection(cid, user, chat)
            items = json.loads(row["items"]) if row else []
            stopped = self.db.execute(
                "SELECT 1 FROM collection_batches WHERE collection_id=? AND state='CANCELLED'",
                (cid,),
            ).fetchone()
            if stopped or not 0 <= index < len(items):
                result = (None, False)
            else:
                existing = self.db.execute(
                    "SELECT job_id FROM collection_selections WHERE collection_id=? AND item_index=?",
                    (cid, index),
                ).fetchone()
                if existing:
                    result = (str(existing[0]), False)
                else:
                    item = items[index]
                    jid = self.create_job(user, chat, item["url"], mode, item)
                    self.db.execute(
                        "INSERT INTO collection_selections VALUES (?,?,?)", (cid, index, jid)
                    )
                    self.db.execute(
                        "INSERT OR IGNORE INTO collection_batches VALUES (?,'ACTIVE')", (cid,)
                    )
                    self.db.execute(
                        "UPDATE collections SET expires=? WHERE id=?",
                        (time.time() + 86400, cid),
                    )
                    result = (jid, True)
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def collection(self, cid: str, user: int, chat: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM collections WHERE id=? AND user_id=? AND chat_id=? AND expires>?",
            (cid, user, chat, time.time()),
        ).fetchone()

    def collection_batch(self, cid: str, user: int, chat: int) -> tuple[int, int, bool]:
        row = self.collection(cid, user, chat)
        if row is None:
            return 0, 0, False
        counts = self.db.execute(
            "SELECT count(*),sum(CASE WHEN j.state IN ('QUEUED','RETRYABLE','RESOLVING',"
            "'DOWNLOADING','VERIFYING') THEN 1 ELSE 0 END) "
            "FROM collection_selections s JOIN jobs j ON j.id=s.job_id "
            "WHERE s.collection_id=?", (cid,),
        ).fetchone()
        stopped = self.db.execute(
            "SELECT state FROM collection_batches WHERE collection_id=?", (cid,)
        ).fetchone()
        return int(counts[0]), int(counts[1] or 0), bool(stopped and stopped[0] == 'CANCELLED')

    def cancel_collection(self, cid: str, user: int, chat: int, message: int) -> int | None:
        """Stop undispatched items only; let the current item complete."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.collection(cid, user, chat)
            if row is None or row['message_id'] != message:
                self.db.execute("COMMIT")
                return None
            self.db.execute(
                "INSERT INTO collection_batches VALUES (?,'CANCELLED') "
                "ON CONFLICT(collection_id) DO UPDATE SET state='CANCELLED'", (cid,),
            )
            changed = self.db.execute(
                "UPDATE jobs SET state='CANCELLED',updated=? WHERE id IN "
                "(SELECT job_id FROM collection_selections WHERE collection_id=?) "
                "AND state IN ('QUEUED','RETRYABLE')", (time.time(), cid),
            ).rowcount
            self.db.execute("COMMIT")
            return changed
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def job(self, jid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()

    def next_jobs(self) -> list[sqlite3.Row]:
        return list(
            self.db.execute("""WITH eligible AS (
              SELECT j.*, ROW_NUMBER() OVER (
                PARTITION BY j.user_id
                ORDER BY CASE WHEN s.collection_id IS NULL THEN 0 ELSE 1 END,
                         j.created,j.id) AS user_priority
              FROM jobs j LEFT JOIN collection_selections s ON s.job_id=j.id
              WHERE j.state IN ('QUEUED','RETRYABLE')
                AND (s.collection_id IS NULL OR NOT EXISTS (
                  SELECT 1 FROM collection_selections other
                  JOIN jobs running ON running.id=other.job_id
                  WHERE other.collection_id=s.collection_id
                    AND running.state IN ('RESOLVING','DOWNLOADING','VERIFYING','DELIVERING')))
            ) SELECT * FROM eligible WHERE user_priority<=6
              ORDER BY user_priority,created,id""")
        )

    def jobs(self, user: int) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM jobs WHERE user_id=? ORDER BY created DESC LIMIT 10", (user,)
            )
        )

    def transition(self, jid: str, from_state: str, to_state: str, **fields: Any) -> bool:
        allowed = {"error", "artifact", "receipt_id"}
        if fields.keys() - allowed:
            raise ValueError("unknown job field")
        assigns = ", ".join(f"{k}=?" for k in fields)
        extra = (", " + assigns) if assigns else ""
        values = [to_state, time.time(), *fields.values(), jid, from_state]
        cur = self.db.execute(
            f"UPDATE jobs SET state=?, updated=?{extra} WHERE id=? AND state=?", values
        )
        return cur.rowcount == 1

    def cancel(self, jid: str, user: int) -> bool:
        cur = self.db.execute(
            "UPDATE jobs SET state='CANCELLED', updated=? WHERE id=? AND user_id=? AND state IN ('QUEUED','RESOLVING','DOWNLOADING','VERIFYING')",
            (time.time(), jid, user),
        )
        return cur.rowcount == 1

    def receipt(self, jid: str, message_id: int, file_id: str | None) -> None:
        self.db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?,?)", (jid, message_id, file_id))

    def recover(self) -> None:
        self.db.execute("UPDATE jobs SET state='DELIVERY_UNCERTAIN' WHERE state='DELIVERING'")
        self.db.execute(
            "UPDATE jobs SET state='RETRYABLE' WHERE state IN ('RESOLVING','DOWNLOADING','VERIFYING')"
        )

    def prompt_query(self, user: int, chat: int, mode: str, ttl: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO query_prompts VALUES (?,?,?,?)",
            (user, chat, mode, time.time() + ttl),
        )

    def take_query(self, user: int, chat: int) -> str | None:
        row = self.db.execute(
            "SELECT mode,expires FROM query_prompts WHERE user_id=? AND chat_id=?", (user, chat)
        ).fetchone()
        self.db.execute("DELETE FROM query_prompts WHERE user_id=? AND chat_id=?", (user, chat))
        return str(row["mode"]) if row and row["expires"] > time.time() else None

    def clear_query(self, user: int, chat: int) -> None:
        self.db.execute("DELETE FROM query_prompts WHERE user_id=? AND chat_id=?", (user, chat))

    def create_search(
        self, user: int, chat: int, query: str, kinds: set[str], sources: set[str]
    ) -> str:
        sid = secrets.token_urlsafe(9)
        self.db.execute(
            "INSERT INTO search_sessions VALUES (?,?,?,NULL,?,?,?,0,0,?)",
            (
                sid,
                user,
                chat,
                query,
                json.dumps(sorted(kinds)),
                json.dumps(sorted(sources)),
                time.time() + 900,
            ),
        )
        return sid

    def search(self, sid: str, user: int, chat: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM search_sessions WHERE id=? AND user_id=? AND chat_id=? AND expires>?",
            (sid, user, chat, time.time()),
        ).fetchone()

    def set_search_message(self, sid: str, mid: int) -> None:
        self.db.execute("UPDATE search_sessions SET message_id=? WHERE id=?", (mid, sid))

    def update_search(self, sid: str, kinds: set[str], sources: set[str], page: int = 0) -> int:
        self.db.execute(
            "UPDATE search_sessions SET kinds=?,sources=?,page=?,generation=generation+1 WHERE id=?",
            (json.dumps(sorted(kinds)), json.dumps(sorted(sources)), page, sid),
        )
        return int(
            self.db.execute("SELECT generation FROM search_sessions WHERE id=?", (sid,)).fetchone()[
                0
            ]
        )

    def save_results(self, sid: str, generation: int, hits: list[dict[str, Any]]) -> None:
        self.db.execute("DELETE FROM results WHERE session_id=?", (sid,))
        self.db.executemany(
            "INSERT INTO results VALUES (?,?,?,?)",
            ((sid, generation, i, json.dumps(h)) for i, h in enumerate(hits)),
        )

    def result(self, sid: str, generation: int, position: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT payload FROM results WHERE session_id=? AND generation=? AND position=?",
            (sid, generation, position),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def results(self, sid: str, generation: int) -> list[dict[str, Any]]:
        return [
            json.loads(x[0])
            for x in self.db.execute(
                "SELECT payload FROM results WHERE session_id=? AND generation=? ORDER BY position",
                (sid, generation),
            )
        ]
