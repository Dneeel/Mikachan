"""Persisted Mikachan principals and group grants, independent of Telegram badges."""

import sqlite3
import time
import secrets
from contextlib import contextmanager
from collections.abc import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
 user_id INTEGER PRIMARY KEY, internal_name TEXT NOT NULL, role TEXT NOT NULL
 CHECK(role IN ('OWNER','ADMIN','USER')), status TEXT NOT NULL
 CHECK(status IN ('ACTIVE','SUSPENDED','REVOKED')),
 suspended_until REAL, reason TEXT NOT NULL DEFAULT '',
 created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS group_registry (
 chat_id INTEGER PRIMARY KEY, internal_name TEXT NOT NULL, status TEXT NOT NULL
 CHECK(status IN ('ACTIVE','SUSPENDED','REVOKED')),
 created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS admin_grants (
 user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 created REAL NOT NULL, actor_id INTEGER NOT NULL,
 PRIMARY KEY(user_id,chat_id));
CREATE TABLE IF NOT EXISTS access_audit (
 id INTEGER PRIMARY KEY, created REAL NOT NULL, actor_id INTEGER NOT NULL,
 action TEXT NOT NULL, user_id INTEGER, chat_id INTEGER, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS access_bootstrap (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 primary_owner_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS module_policies (
 domain TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)));
CREATE TABLE IF NOT EXISTS private_access (
 user_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)));
CREATE TABLE IF NOT EXISTS owner_requests (
 token TEXT PRIMARY KEY, requester_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
 action TEXT NOT NULL CHECK(action IN ('PROMOTE','REVOKE')),
 created REAL NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL
 CHECK(status IN ('PENDING','APPROVED','EXPIRED')));
CREATE TABLE IF NOT EXISTS coowner_origins (
 user_id INTEGER PRIMARY KEY, prior_role TEXT NOT NULL CHECK(prior_role IN ('USER','ADMIN')));
"""

MODULES = frozenset({"downloader", "community", "utilities", "follow", "content"})


class AccessRegistry:
    def __init__(self, db: sqlite3.Connection, primary_owner: int):
        if not 0 < primary_owner < 2**63:
            raise ValueError("invalid primary owner")
        self.db = db
        db.executescript(SCHEMA)
        row = db.execute("SELECT primary_owner_id FROM access_bootstrap WHERE singleton=1").fetchone()
        if row and int(row[0]) != primary_owner:
            raise ValueError("primary owner differs from persisted protected owner")
        self.primary_owner = primary_owner
        if row:
            protected = self.person(primary_owner)
            if not protected or protected["role"] != "OWNER" or protected["status"] != "ACTIVE":
                raise ValueError("protected primary Owner registry is inconsistent")
            return
        now = time.time()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO access_bootstrap VALUES (1,?)", (primary_owner,))
            db.execute("INSERT OR IGNORE INTO access VALUES (?)", (primary_owner,))
            for item in db.execute("SELECT user_id FROM access").fetchall():
                uid = int(item[0])
                if uid <= 0:
                    raise ValueError("invalid legacy user ID")
                role = "OWNER" if uid == primary_owner else "USER"
                name = "Primary Owner" if uid == primary_owner else f"Legacy user {uid}"
                db.execute("INSERT OR IGNORE INTO people VALUES (?,?,?,?,NULL,'',?,?)",
                           (uid, name, role, "ACTIVE", now, now))
            for item in db.execute("SELECT chat_id FROM allowed_groups").fetchall():
                gid = int(item[0])
                if gid >= 0:
                    raise ValueError("invalid legacy group ID")
                db.execute("INSERT OR IGNORE INTO group_registry VALUES (?,?,?,?,?)",
                           (gid, f"Legacy group {gid}", "ACTIVE", now, now))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def person(self, user_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM people WHERE user_id=?", (user_id,)).fetchone()

    def role(self, user_id: int) -> str | None:
        row = self.person(user_id)
        if not row or row["status"] != "ACTIVE":
            if not row or row["status"] != "SUSPENDED" or not row["suspended_until"] \
                    or row["suspended_until"] > time.time():
                return None
            self.db.execute("UPDATE people SET status='ACTIVE', suspended_until=NULL, "
                            "reason='',updated=? WHERE user_id=? AND status='SUSPENDED' "
                            "AND suspended_until<=?", (time.time(), user_id, time.time()))
            row = self.person(user_id)
        return str(row["role"]) if row and row["status"] == "ACTIVE" else None

    def groups(self) -> set[int]:
        return {int(r[0]) for r in self.db.execute("SELECT r.chat_id FROM group_registry r "
                    "JOIN allowed_groups g ON g.chat_id=r.chat_id WHERE r.status='ACTIVE'")}

    def allowlist(self) -> set[int]:
        return {int(r[0]) for r in self.db.execute("SELECT user_id FROM people "
                    "WHERE status='ACTIVE' OR (status='SUSPENDED' AND suspended_until<=?)",
                    (time.time(),))}

    def can_access(self, user: int, chat: int, chat_type: str) -> bool:
        role = self.role(user)
        if role is None:
            return False
        if chat_type == "private":
            if chat != user:
                return False
            if role == "OWNER":
                return True
            row = self.db.execute("SELECT enabled FROM private_access WHERE user_id=?",
                                  (user,)).fetchone()
            return bool(row[0]) if row else False
        return chat_type in {"group", "supergroup"} and chat in self.groups()

    def can_manage(self, user: int, chat: int) -> bool:
        if chat not in self.groups():
            return False
        role = self.role(user)
        if role == "OWNER":
            return True
        return role == "ADMIN" and bool(self.db.execute(
            "SELECT 1 FROM admin_grants WHERE user_id=? AND chat_id=?", (user, chat)).fetchone())

    def audit(self, actor: int, action: str, user: int | None = None,
              chat: int | None = None, detail: str = "") -> None:
        self.db.execute("INSERT INTO access_audit(created,actor_id,action,user_id,chat_id,detail) "
                        "VALUES (?,?,?,?,?,?)", (time.time(), actor, action, user, chat, detail[:200]))

    def private_enabled(self, user: int) -> bool:
        if user == self.primary_owner:
            return True
        row = self.db.execute("SELECT enabled FROM private_access WHERE user_id=?",
                              (user,)).fetchone()
        return bool(row[0]) if row else False

    def set_private(self, actor: int, user: int, enabled: bool) -> None:
        if self.role(actor) != "OWNER" or user == self.primary_owner or not self.person(user):
            raise ValueError("Only an Owner can change another registered person's private access")
        with self._transaction():
            self.db.execute("INSERT INTO private_access VALUES (?,?) ON CONFLICT(user_id) "
                            "DO UPDATE SET enabled=excluded.enabled", (user, int(enabled)))
            self.audit(actor, "private_access", user, detail="on" if enabled else "off")

    def module_enabled(self, domain: str) -> bool:
        if domain not in MODULES:
            return False
        row = self.db.execute("SELECT enabled FROM module_policies WHERE domain=?",
                              (domain,)).fetchone()
        return bool(row[0]) if row else True

    def set_module(self, actor: int, domain: str, enabled: bool) -> None:
        if self.role(actor) != "OWNER" or domain not in MODULES:
            raise ValueError("Owner authorization and a registered module are required")
        with self._transaction():
            self.db.execute("INSERT INTO module_policies VALUES (?,?) "
                            "ON CONFLICT(domain) DO UPDATE SET enabled=excluded.enabled",
                            (domain, int(enabled)))
            self.audit(actor, "module_policy", detail=f"{domain}={'on' if enabled else 'off'}")

    def request_owner_change(self, actor: int, target: int, action: str) -> str:
        if actor != self.primary_owner or self.role(actor) != "OWNER":
            raise ValueError("Only the protected primary Owner may request Owner changes")
        person = self.person(target)
        if target == actor or not person or person["status"] != "ACTIVE":
            raise ValueError("Target must be a different active registered person")
        if action == "PROMOTE" and person["role"] not in {"USER", "ADMIN"} or \
                action == "REVOKE" and person["role"] != "OWNER":
            raise ValueError("Owner change is not applicable to this person")
        token = secrets.token_urlsafe(18)
        now = time.time()
        with self._transaction():
            self.db.execute("INSERT INTO owner_requests VALUES (?,?,?,?,?,?,'PENDING')",
                            (token, actor, target, action, now, now + 86400))
            self.audit(actor, "owner_request", target, detail=action)
        return token

    def set_coowner(self, actor: int, target: int, enabled: bool) -> None:
        """The protected primary owner directly manages co-owners in Telegram."""
        if actor != self.primary_owner or self.role(actor) != "OWNER" or target == actor:
            raise ValueError("Only the protected primary Owner can change co-owners")
        person = self.person(target)
        if not person or person["status"] != "ACTIVE" or \
                (enabled and person["role"] not in {"USER", "ADMIN"}) or \
                (not enabled and person["role"] != "OWNER"):
            raise ValueError("Target must be an active registered person with the expected role")
        with self._transaction():
            if enabled:
                self.db.execute("INSERT INTO coowner_origins VALUES (?,?) "
                                "ON CONFLICT(user_id) DO NOTHING", (target, person["role"]))
                role = "OWNER"
            else:
                original = self.db.execute("SELECT prior_role FROM coowner_origins "
                                           "WHERE user_id=?", (target,)).fetchone()
                role = str(original[0]) if original else "USER"
                self.db.execute("DELETE FROM coowner_origins WHERE user_id=?", (target,))
            self.db.execute("UPDATE people SET role=?,updated=? WHERE user_id=?",
                            (role, time.time(), target))
            self.audit(actor, "coowner_promote" if enabled else "coowner_revoke", target)

    def register(self, actor: int, user: int, name: str, role: str = "USER") -> None:
        if self.role(actor) != "OWNER" or role not in {"USER", "ADMIN"}:
            raise ValueError("Owner authorization required")
        if not 0 < user < 2**63 or not 1 <= len(name.strip()) <= 80:
            raise ValueError("Enter a positive numeric ID and an internal name (1–80 characters)")
        if self.person(user):
            raise ValueError("This ID is already registered; use the edit action")
        now = time.time()
        with self._transaction():
            self.db.execute("INSERT INTO people VALUES (?,?,?,?,NULL,'',?,?)",
                            (user, name.strip(), role, "ACTIVE", now, now))
            self.db.execute("INSERT OR IGNORE INTO access VALUES (?)", (user,))
            self.audit(actor, "register", user, detail=role)

    def update_person(self, actor: int, user: int, *, name: str | None = None,
                      role: str | None = None, status: str | None = None,
                      suspended_until: float | None = None, reason: str = "") -> None:
        if self.role(actor) != "OWNER":
            raise ValueError("Owner authorization required")
        row = self.person(user)
        if not row:
            raise ValueError("Unknown person")
        if user == self.primary_owner and (role not in {None, "OWNER"} or
                                            status in {"SUSPENDED", "REVOKED"}):
            raise ValueError("Protected primary Owner cannot be demoted or disabled")
        if row["role"] == "OWNER" and (role not in {None, "OWNER"} or status == "REVOKED"):
            raise ValueError("Owner changes require independent operator approval")
        if row["status"] == "REVOKED" and status is not None:
            raise ValueError("Revoked IDs need a new operator-reviewed registration")
        if role not in {None, "USER", "ADMIN"}:
            raise ValueError("Owner promotion requires independent operator approval")
        if status not in {None, "ACTIVE", "SUSPENDED", "REVOKED"}:
            raise ValueError("Invalid status")
        if name is not None and not 1 <= len(name.strip()) <= 80:
            raise ValueError("Internal name must be 1–80 characters")
        now = time.time()
        with self._transaction():
            self.db.execute("UPDATE people SET internal_name=?,role=?,status=?, "
                            "suspended_until=?,reason=?,updated=? WHERE user_id=?",
                            (name.strip() if name is not None else row["internal_name"],
                             role or row["role"], status or row["status"],
                             (suspended_until if status == "SUSPENDED" else None)
                             if status is not None else row["suspended_until"],
                             (reason[:200] if status == "SUSPENDED" else "")
                             if status is not None else row["reason"], now, user))
            if role == "USER" or status == "REVOKED":
                self.db.execute("DELETE FROM admin_grants WHERE user_id=?", (user,))
            if status == "REVOKED":
                self.db.execute("DELETE FROM access WHERE user_id=?", (user,))
            if status in {"SUSPENDED", "REVOKED"}:
                self.db.execute("UPDATE jobs SET state='CANCELLED',updated=? WHERE user_id=? "
                                "AND state IN ('QUEUED','RESOLVING','DOWNLOADING','VERIFYING')",
                                (now, user))
            self.audit(actor, "update_person", user, detail=f"role={role};status={status}")

    def add_group(self, actor: int, chat: int, name: str) -> None:
        if self.role(actor) != "OWNER" or not -2**63 < chat < 0 or not 1 <= len(name.strip()) <= 80:
            raise ValueError("Owner, negative group ID and internal name required")
        if self.db.execute("SELECT 1 FROM group_registry WHERE chat_id=?", (chat,)).fetchone():
            raise ValueError("Group is already registered")
        now = time.time()
        with self._transaction():
            self.db.execute("INSERT INTO group_registry VALUES (?,?,?,?,?)",
                            (chat, name.strip(), "ACTIVE", now, now))
            self.db.execute("INSERT OR IGNORE INTO allowed_groups VALUES (?)", (chat,))
            self.audit(actor, "add_group", chat=chat)

    def set_group_status(self, actor: int, chat: int, status: str) -> None:
        if self.role(actor) != "OWNER" or status not in {"ACTIVE", "SUSPENDED", "REVOKED"}:
            raise ValueError("Owner authorization required")
        if not self.db.execute("SELECT 1 FROM group_registry WHERE chat_id=?", (chat,)).fetchone():
            raise ValueError("Unknown group")
        current = self.db.execute("SELECT status FROM group_registry WHERE chat_id=?",
                                  (chat,)).fetchone()
        if current and current[0] == "REVOKED" and status != "REVOKED":
            raise ValueError("Revoked group requires an operator-reviewed replacement")
        with self._transaction():
            self.db.execute("UPDATE group_registry SET status=?,updated=? WHERE chat_id=?",
                            (status, time.time(), chat))
            if status != "ACTIVE":
                self.db.execute("UPDATE jobs SET state='CANCELLED',updated=? WHERE chat_id=? "
                                "AND state IN ('QUEUED','RESOLVING','DOWNLOADING','VERIFYING')",
                                (time.time(), chat))
            self.audit(actor, "group_status", chat=chat, detail=status)

    def assign(self, actor: int, user: int, chat: int, grant: bool) -> None:
        target = self.person(user)
        if (self.role(actor) != "OWNER" or not target or target["role"] != "ADMIN" or
                (grant and target["status"] != "ACTIVE") or chat not in self.groups()):
            raise ValueError("An Owner may assign an active Admin to an approved group")
        with self._transaction():
            if grant:
                self.db.execute("INSERT OR IGNORE INTO admin_grants VALUES (?,?,?,?)",
                                (user, chat, time.time(), actor))
            else:
                self.db.execute("DELETE FROM admin_grants WHERE user_id=? AND chat_id=?",
                                (user, chat))
            self.audit(actor, "assign_admin" if grant else "remove_admin", user, chat)
