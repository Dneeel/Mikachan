"""Small transactional SQLite store. Callback decisions are compare-and-swap operations."""

import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

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
CREATE TABLE IF NOT EXISTS admin_pending (
 id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 message_id INTEGER, target_id INTEGER NOT NULL, action TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS preferences (
 user_id INTEGER PRIMARY KEY, kinds TEXT NOT NULL, sources TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collection_selections (
 collection_id TEXT NOT NULL, item_index INTEGER NOT NULL, job_id TEXT NOT NULL,
 PRIMARY KEY(collection_id,item_index));
CREATE TABLE IF NOT EXISTS collections (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 message_id INTEGER, title TEXT NOT NULL, items TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cache (
 key TEXT PRIMARY KEY, file_id TEXT NOT NULL, media_kind TEXT NOT NULL,
 owner_id INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS job_modes (
 job_id TEXT PRIMARY KEY, mode TEXT NOT NULL);
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
        return {int(row[0]) for row in self.db.execute("SELECT user_id FROM access")}

    def propose_access(self, owner: int, chat: int, target: int, action: str) -> str:
        if not 0 < target < 2**63 or action not in {"add", "remove"}:
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
            elif row["action"] == "add" and len(self.allowlist()) >= 4:
                result = "maximum four users"
            else:
                if row["action"] == "add":
                    self.db.execute("INSERT OR IGNORE INTO access VALUES (?)", (row["target_id"],))
                else:
                    self.db.execute("DELETE FROM access WHERE user_id=?", (row["target_id"],))
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
        self, cid: str, user: int, chat: int, prompt: int, yes: bool
    ) -> tuple[str, str | None]:
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
                result = (state, jid)
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def create_job(self, user: int, chat: int, url: str) -> str:
        jid = secrets.token_urlsafe(9)
        now = time.time()
        self.db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,NULL)",
            (jid, user, chat, url, "QUEUED", now, now, None, None),
        )
        return jid

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
                    jid = self.create_job(user, chat, str(parent["url"]))
                    self.db.execute("INSERT INTO job_modes VALUES (?,?)", (jid, mode))
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
        self, user: int, chat: int, title: str, items: list[dict[str, str]]
    ) -> str:
        cid = secrets.token_urlsafe(9)
        self.db.execute(
            "INSERT INTO collections VALUES (?,?,?,NULL,?,?,?)",
            (cid, user, chat, title[:200], json.dumps(items), time.time() + 1800),
        )
        return cid

    def select_collection(
        self, cid: str, user: int, chat: int, index: int
    ) -> tuple[str | None, bool]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result: tuple[str | None, bool]
            row = self.collection(cid, user, chat)
            items = json.loads(row["items"]) if row else []
            if not 0 <= index < len(items):
                result = (None, False)
            else:
                existing = self.db.execute(
                    "SELECT job_id FROM collection_selections WHERE collection_id=? AND item_index=?",
                    (cid, index),
                ).fetchone()
                if existing:
                    result = (str(existing[0]), False)
                else:
                    jid = self.create_job(user, chat, items[index]["url"])
                    self.db.execute(
                        "INSERT INTO collection_selections VALUES (?,?,?)", (cid, index, jid)
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

    def job(self, jid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()

    def next_jobs(self) -> list[sqlite3.Row]:
        return list(
            self.db.execute("""SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created,id) AS priority
            FROM jobs WHERE state IN ('QUEUED','RETRYABLE')
        ) WHERE priority=1 ORDER BY created LIMIT 4""")
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
