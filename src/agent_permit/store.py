"""Permit store: requests, decisions and grants in SQLite, plus a hash-chained audit log.

A permit is bound to one exact action. The action is a JSON-serialisable value (for a
tool call: the tool name and its arguments). Its fingerprint is the SHA-256 of the
canonical JSON, so an approval for ``git push origin main`` does not cover
``git push --force origin main``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator, List, Optional

PENDING, APPROVED, DENIED, EXPIRED, USED = "pending", "approved", "denied", "expired", "used"

SCHEMA = """
CREATE TABLE IF NOT EXISTS permits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT NOT NULL,
    agent        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    action_json  TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   REAL NOT NULL,
    request_expires_at REAL NOT NULL,
    decided_at   REAL,
    decided_by   TEXT,
    grant_expires_at REAL,
    uses_left    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS permits_fp ON permits (fingerprint, status);
CREATE TABLE IF NOT EXISTS freeze (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    active  INTEGER NOT NULL DEFAULT 0,
    reason  TEXT,
    by_whom TEXT,
    since   REAL
);
INSERT OR IGNORE INTO freeze (id, active) VALUES (1, 0);
"""


def canonical(action: Any) -> str:
    return json.dumps(action, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(action: Any) -> str:
    return hashlib.sha256(canonical(action).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Permit:
    id: int
    fingerprint: str
    agent: str
    summary: str
    action: Any
    status: str
    created_at: float
    request_expires_at: float
    decided_at: Optional[float]
    decided_by: Optional[str]
    grant_expires_at: Optional[float]
    uses_left: int

    @property
    def short(self) -> str:
        return self.fingerprint[:12]


class AuditLog:
    """Append-only JSON Lines file where every entry carries the hash of the previous one.

    Editing or deleting a line breaks the chain, and :meth:`verify` reports where.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _last_hash(self) -> str:
        if not os.path.exists(self.path):
            return "0" * 64
        last = None
        with open(self.path, "rb") as f:
            for line in f:
                if line.strip():
                    last = line
        return json.loads(last)["hash"] if last else "0" * 64

    def append(self, event: str, **fields: Any) -> dict:
        with self._lock:
            entry = {"ts": round(time.time(), 3), "event": event, **fields, "prev": self._last_hash()}
            entry["hash"] = hashlib.sha256(canonical(entry).encode("utf-8")).hexdigest()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return entry

    def entries(self) -> Iterator[dict]:
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    def verify(self) -> tuple[bool, int, str]:
        """Return (ok, entries checked, message)."""
        prev = "0" * 64
        n = 0
        for n, entry in enumerate(self.entries(), 1):
            claimed = entry.get("hash")
            body = {k: v for k, v in entry.items() if k != "hash"}
            if body.get("prev") != prev:
                return False, n, f"entry {n}: chain broken (prev does not match entry {n - 1})"
            if hashlib.sha256(canonical(body).encode("utf-8")).hexdigest() != claimed:
                return False, n, f"entry {n}: content does not match its hash"
            prev = claimed
        return True, n, f"{n} entries, chain intact"


class Store:
    """Thread-safe permit store. One SQLite file, one audit log next to it."""

    def __init__(self, path: str, audit_path: Optional[str] = None) -> None:
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.audit = AuditLog(audit_path or os.path.join(d, "audit.jsonl"))
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    @staticmethod
    def _row(r: sqlite3.Row) -> Permit:
        return Permit(
            id=r["id"],
            fingerprint=r["fingerprint"],
            agent=r["agent"],
            summary=r["summary"],
            action=json.loads(r["action_json"]),
            status=r["status"],
            created_at=r["created_at"],
            request_expires_at=r["request_expires_at"],
            decided_at=r["decided_at"],
            decided_by=r["decided_by"],
            grant_expires_at=r["grant_expires_at"],
            uses_left=r["uses_left"],
        )

    def _expire(self, c: sqlite3.Connection, now: float) -> None:
        for r in c.execute(
            "SELECT id FROM permits WHERE (status=? AND request_expires_at<=?) OR (status=? AND grant_expires_at<=?)",
            (PENDING, now, APPROVED, now),
        ).fetchall():
            c.execute("UPDATE permits SET status=? WHERE id=?", (EXPIRED, r["id"]))
            self.audit.append("expired", id=r["id"])

    # ------------------------------------------------------------- requests

    def request(self, action: Any, summary: str, agent: str = "agent", request_ttl: float = 6 * 3600) -> tuple[Permit, bool]:
        """Create a pending request, or return the open one for the same action.

        Returns (permit, created). An identical action that is already pending or
        approved is not requested twice.
        """
        fp = fingerprint(action)
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire(c, now)
            r = c.execute(
                "SELECT * FROM permits WHERE fingerprint=? AND status IN (?,?) ORDER BY id DESC LIMIT 1",
                (fp, PENDING, APPROVED),
            ).fetchone()
            if r:
                c.execute("COMMIT")
                return self._row(r), False
            cur = c.execute(
                "INSERT INTO permits (fingerprint, agent, summary, action_json, status, created_at, request_expires_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (fp, agent, summary, canonical(action), PENDING, now, now + request_ttl),
            )
            pid = cur.lastrowid
            c.execute("COMMIT")
        self.audit.append("requested", id=pid, fingerprint=fp, agent=agent, summary=summary)
        return self.get(pid), True

    def get(self, pid: int) -> Optional[Permit]:
        with self._conn() as c:
            self._expire(c, time.time())
            r = c.execute("SELECT * FROM permits WHERE id=?", (pid,)).fetchone()
            return self._row(r) if r else None

    def list(self, status: Optional[str] = None, limit: int = 50) -> List[Permit]:
        with self._conn() as c:
            self._expire(c, time.time())
            if status:
                rows = c.execute("SELECT * FROM permits WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
            else:
                rows = c.execute("SELECT * FROM permits ORDER BY id DESC LIMIT ?", (limit,))
            return [self._row(r) for r in rows.fetchall()]

    # ------------------------------------------------------------ decisions

    def decide(self, pid: int, approve: bool, by: str, grant_ttl: float = 30 * 60, uses: int = 1) -> Permit:
        """Approve or deny a pending request. Only pending requests can be decided."""
        if approve and (grant_ttl <= 0 or uses < 1):
            raise ValueError("an approval needs a positive grant_ttl and at least one use")
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire(c, now)
            r = c.execute("SELECT status FROM permits WHERE id=?", (pid,)).fetchone()
            if r is None:
                c.execute("ROLLBACK")
                raise KeyError(f"no permit {pid}")
            if r["status"] != PENDING:
                c.execute("ROLLBACK")
                raise ValueError(f"permit {pid} is {r['status']}, not pending")
            if approve:
                c.execute(
                    "UPDATE permits SET status=?, decided_at=?, decided_by=?, grant_expires_at=?, uses_left=? WHERE id=?",
                    (APPROVED, now, by, now + grant_ttl, uses, pid),
                )
            else:
                c.execute("UPDATE permits SET status=?, decided_at=?, decided_by=? WHERE id=?", (DENIED, now, by, pid))
            c.execute("COMMIT")
        self.audit.append("approved" if approve else "denied", id=pid, by=by,
                          grant_ttl=grant_ttl if approve else None, uses=uses if approve else None)
        return self.get(pid)

    # --------------------------------------------------------------- freeze

    def freeze(self, reason: str, by: str) -> dict:
        """Emergency stop. While active, the hook blocks every tool call and the MCP tools
        grant nothing, whatever permits exist. Only a human unfreezes."""
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute("UPDATE freeze SET active=1, reason=?, by_whom=?, since=? WHERE id=1", (reason[:300], by, now))
        self.audit.append("freeze", reason=reason[:300], by=by)
        return {"active": True, "reason": reason[:300], "by": by, "since": now}

    def unfreeze(self, by: str) -> dict:
        with self._lock, self._conn() as c:
            c.execute("UPDATE freeze SET active=0 WHERE id=1")
        self.audit.append("unfreeze", by=by)
        return {"active": False}

    def frozen(self) -> Optional[dict]:
        """The active freeze as a dict, or None."""
        with self._conn() as c:
            r = c.execute("SELECT active, reason, by_whom, since FROM freeze WHERE id=1").fetchone()
        if r is None or not r["active"]:
            return None
        return {"active": True, "reason": r["reason"], "by": r["by_whom"], "since": r["since"]}

    def consume(self, action: Any, agent: str = "agent") -> Optional[Permit]:
        """Use one approval for exactly this action. Returns the permit, or None if there is none
        or the store is frozen (a freeze beats every permit)."""
        if self.frozen():
            self.audit.append("frozen_refusal", fingerprint=fingerprint(action), agent=agent)
            return None
        fp = fingerprint(action)
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire(c, now)
            r = c.execute(
                "SELECT * FROM permits WHERE fingerprint=? AND status=? AND uses_left>0 ORDER BY id ASC LIMIT 1",
                (fp, APPROVED),
            ).fetchone()
            if r is None:
                c.execute("COMMIT")
                return None
            left = r["uses_left"] - 1
            c.execute("UPDATE permits SET uses_left=?, status=? WHERE id=?", (left, APPROVED if left else USED, r["id"]))
            c.execute("COMMIT")
        self.audit.append("used", id=r["id"], fingerprint=fp, agent=agent, uses_left=left)
        return self.get(r["id"])
