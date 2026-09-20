"""System-of-record port and a SQLite implementation.

SQLite is suitable for local runs and pilots. For production, implement `Repository` on the
approved store (Dataverse, Azure SQL, Cosmos DB); the request-ID sequence must be atomic there.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Protocol

from .models import (
    Architect,
    ArchitectLoad,
    EmailMessage,
    RequestRecord,
    utcnow,
)
from .taxonomy import INACTIVE_STATUSES, RequestStatus


class Repository(Protocol):
    def allocate_request_id(self) -> str: ...
    def save_request(self, record: RequestRecord) -> None: ...
    def get_request(self, request_id: str) -> RequestRecord | None: ...
    def find_by_email(self, msg: EmailMessage) -> RequestRecord | None: ...
    def find_by_conversation(self, conversation_id: str) -> list[RequestRecord]: ...
    def list_requests(self, *, active_only: bool = False, status: RequestStatus | None = None) -> list[RequestRecord]: ...
    def architect_loads(self) -> dict[str, ArchitectLoad]: ...
    def was_processed(self, email_key: str) -> bool: ...
    def mark_processed(self, email_key: str, outcome: str, request_id: str | None) -> None: ...
    def add_status_history(self, request_id: str, previous: RequestStatus | None, new: RequestStatus, ts: datetime, initiated_by: str, reason: str | None) -> None: ...
    def add_audit(self, entry: dict[str, Any]) -> None: ...
    def add_assignment_history(self, entry: dict[str, Any]) -> None: ...
    def add_skills(self, request_id: str, rows: list[dict[str, Any]]) -> None: ...
    def add_communication(self, entry: dict[str, Any]) -> None: ...
    def has_communication(self, request_id: str, comm_type: str, recipient: str, dedupe_key: str) -> bool: ...
    def events(self, kind: str, request_id: str | None = None) -> list[dict[str, Any]]: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests(
    request_id TEXT PRIMARY KEY, email_key TEXT, conversation_id TEXT,
    status TEXT NOT NULL, assigned TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_requests_email ON requests(email_key);
CREATE INDEX IF NOT EXISTS ix_requests_conv ON requests(conversation_id);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, request_id TEXT,
    dedupe_key TEXT, ts TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_events_kind ON events(kind, request_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_comm ON events(kind, request_id, dedupe_key)
    WHERE kind = 'communication';
CREATE TABLE IF NOT EXISTS processed(
    email_key TEXT PRIMARY KEY, outcome TEXT NOT NULL, request_id TEXT, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seq(year INTEGER PRIMARY KEY, n INTEGER NOT NULL);
"""


def _dump(obj: Any) -> str:
    return json.dumps(obj, default=str, sort_keys=True)


class SqliteRepository:
    def __init__(self, path: str = ":memory:", now=utcnow):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._now = now
        with self._lock:
            self._db.executescript(_SCHEMA)

    # -- identifiers ------------------------------------------------------------
    def allocate_request_id(self) -> str:
        year = self._now().year
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("INSERT OR IGNORE INTO seq(year, n) VALUES(?, 0)", (year,))
                self._db.execute("UPDATE seq SET n = n + 1 WHERE year = ?", (year,))
                n = self._db.execute("SELECT n FROM seq WHERE year = ?", (year,)).fetchone()["n"]
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return f"CIS-{year}-{n:05d}"

    # -- requests ---------------------------------------------------------------
    def save_request(self, record: RequestRecord) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO requests(request_id, email_key, conversation_id, status, assigned, data) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET "
                "email_key=excluded.email_key, conversation_id=excluded.conversation_id, "
                "status=excluded.status, assigned=excluded.assigned, data=excluded.data",
                (
                    record.request_id, record.email_dedupe_key, record.conversation_id,
                    record.status.value, record.assigned_architect_id, record.model_dump_json(),
                ),
            )

    def _load(self, rows) -> list[RequestRecord]:
        return [RequestRecord.model_validate_json(r["data"]) for r in rows]

    def get_request(self, request_id: str) -> RequestRecord | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM requests WHERE request_id = ?", (request_id,)).fetchone()
        return RequestRecord.model_validate_json(row["data"]) if row else None

    def find_by_email(self, msg: EmailMessage) -> RequestRecord | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM requests WHERE email_key = ?", (msg.dedupe_key,)).fetchone()
        return RequestRecord.model_validate_json(row["data"]) if row else None

    def find_by_conversation(self, conversation_id: str) -> list[RequestRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM requests WHERE conversation_id = ? ORDER BY request_id", (conversation_id,)
            ).fetchall()
        return self._load(rows)

    def list_requests(self, *, active_only: bool = False, status: RequestStatus | None = None) -> list[RequestRecord]:
        sql, args = "SELECT data FROM requests", []
        clauses = []
        if status:
            clauses.append("status = ?")
            args.append(status.value)
        if active_only:
            marks = ",".join("?" * len(INACTIVE_STATUSES))
            clauses.append(f"status NOT IN ({marks})")
            args.extend(s.value for s in INACTIVE_STATUSES)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY request_id", args).fetchall()
        return self._load(rows)

    def architect_loads(self) -> dict[str, ArchitectLoad]:
        counts: dict[str, int] = {}
        projects: dict[str, set[str]] = {}
        for r in self.list_requests(active_only=True):
            if r.assigned_architect_id:
                counts[r.assigned_architect_id] = counts.get(r.assigned_architect_id, 0) + 1
                projects.setdefault(r.assigned_architect_id, set()).add(r.project_name.strip().lower())
        return {
            a: ArchitectLoad(active_request_count=n, active_project_count=len(projects[a] - {""}))
            for a, n in counts.items()
        }

    # -- processed-message ledger (idempotency) ---------------------------------
    def was_processed(self, email_key: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM processed WHERE email_key = ?", (email_key,)).fetchone() is not None

    def mark_processed(self, email_key: str, outcome: str, request_id: str | None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO processed(email_key, outcome, request_id, ts) VALUES(?,?,?,?)",
                (email_key, outcome, request_id, self._now().isoformat()),
            )

    # -- append-only events -----------------------------------------------------
    def _event(self, kind: str, request_id: str | None, data: dict[str, Any], dedupe_key: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO events(kind, request_id, dedupe_key, ts, data) VALUES(?,?,?,?,?)",
                (kind, request_id, dedupe_key, self._now().isoformat(), _dump(data)),
            )

    def add_status_history(self, request_id, previous, new, ts, initiated_by, reason) -> None:
        self._event("status_history", request_id, {
            "RequestID": request_id, "PreviousStatus": previous.value if previous else None, "NewStatus": new.value,
            "Timestamp": ts.isoformat(), "InitiatedBy": initiated_by, "Reason": reason,
        })

    def add_audit(self, entry: dict[str, Any]) -> None:
        self._event("audit", entry.get("RequestID"), entry)

    def add_assignment_history(self, entry: dict[str, Any]) -> None:
        self._event("assignment_history", entry.get("RequestID"), entry)

    def add_skills(self, request_id: str, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            self._event("skill", request_id, {"RequestID": request_id, **row})

    def add_communication(self, entry: dict[str, Any]) -> None:
        self._event("communication", entry["RequestID"], entry, entry["DedupeKey"])

    def has_communication(self, request_id: str, comm_type: str, recipient: str, dedupe_key: str) -> bool:
        key = f"{comm_type}|{recipient.lower()}|{dedupe_key}"
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM events WHERE kind='communication' AND request_id=? AND dedupe_key=?",
                (request_id, key),
            ).fetchone() is not None

    def events(self, kind: str, request_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT data FROM events WHERE kind = ?", [kind]
        if request_id:
            sql += " AND request_id = ?"
            args.append(request_id)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id", args).fetchall()
        return [json.loads(r["data"]) for r in rows]


def load_architects(path: str) -> list[Architect]:
    """Architect Capability Matrix from a JSON array. Replace with the approved admin-maintained source."""
    with open(path, encoding="utf-8") as fh:
        return [Architect.model_validate(row) for row in json.load(fh)]
