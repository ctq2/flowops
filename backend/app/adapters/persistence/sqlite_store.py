"""SQLite adapter — the default production store.

Why SQLite and not Postgres here?  Because the whole point of this repository is
that it runs on a reviewer's laptop with `python -m app` and nothing else.  The
adapter still does the things a real store must do: WAL journalling, a
transaction per write, optimistic concurrency, an append-only audit table and
idempotency keys with a unique index.  Swapping in Postgres means implementing
the same ``TicketStore`` protocol and nothing else.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ...application.models import AuditEntry, IdempotencyRecord, Page, TicketQuery
from ...application.ports.store import TicketStore
from ...domain.errors import ConflictError, NotFoundError, PreconditionFailedError
from ...domain.ticket import Ticket
from .memory import decode_cursor, encode_cursor, matches, sort_key, sort_of

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tickets (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    body                TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL,
    priority            TEXT NOT NULL,
    team                TEXT,
    reporter            TEXT NOT NULL,
    assignee            TEXT,
    channel             TEXT NOT NULL DEFAULT 'web',
    tags                TEXT NOT NULL DEFAULT '[]',
    labels              TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    closed_at           TEXT,
    sla_due_at          TEXT,
    sla_duration_hours  REAL,
    sla_consumed_hours  REAL,
    sla_snooze_hours    REAL NOT NULL DEFAULT 0,
    sla_calendar        TEXT NOT NULL DEFAULT 'business',
    escalation_at       TEXT,
    escalation_target   TEXT,
    policy_hash         TEXT,
    required_review     TEXT,
    version             INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_tickets_status      ON tickets (status);
CREATE INDEX IF NOT EXISTS idx_tickets_priority    ON tickets (priority);
CREATE INDEX IF NOT EXISTS idx_tickets_team        ON tickets (team);
CREATE INDEX IF NOT EXISTS idx_tickets_created_at  ON tickets (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_sla_due_at  ON tickets (sla_due_at);

CREATE TABLE IF NOT EXISTS audit_log (
    ticket_id  TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    action     TEXT NOT NULL,
    actor      TEXT NOT NULL,
    at         TEXT NOT NULL,
    changes    TEXT NOT NULL DEFAULT '{}',
    meta       TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (ticket_id, seq),
    FOREIGN KEY (ticket_id) REFERENCES tickets (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key                 TEXT PRIMARY KEY,
    ticket_id           TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    status              INTEGER NOT NULL,
    body                TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

COLUMNS = (
    "id, title, body, status, priority, team, reporter, assignee, channel, tags, labels, "
    "created_at, updated_at, closed_at, sla_due_at, sla_duration_hours, sla_consumed_hours, "
    "sla_snooze_hours, sla_calendar, escalation_at, escalation_target, policy_hash, required_review, version"
)


class SqliteStore(TicketStore):
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def schema_version(self) -> str:
        row = self._conn.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone()
        return row["value"] if row else "0"

    def set_schema_version(self, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (name, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (value,),
            )
            self._conn.commit()

    # -- TicketStore ------------------------------------------------------
    def add(self, ticket: Ticket) -> Ticket:
        with self._lock:
            try:
                self._conn.execute(
                    f"INSERT INTO tickets ({COLUMNS}) VALUES ({', '.join('?' * 24)})",
                    _row_values(ticket),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise ConflictError(f"ticket {ticket.id} already exists") from exc
            return ticket

    def get(self, ticket_id: str) -> Ticket | None:
        with self._lock:
            row = self._conn.execute(f"SELECT {COLUMNS} FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return _to_ticket(row) if row else None

    def save(self, ticket: Ticket, *, expected_version: int | None = None) -> Ticket:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute("SELECT version FROM tickets WHERE id = ?", (ticket.id,)).fetchone()
                if row is None:
                    raise NotFoundError(f"ticket {ticket.id} does not exist")
                current_version = int(row["version"])
                if expected_version is not None and current_version != expected_version:
                    raise PreconditionFailedError(
                        "the ticket changed since you loaded it",
                        expected_version=expected_version,
                        current_version=current_version,
                    )
                rebased = replace(ticket, version=current_version + 1)
                # `id` is the key, so it is excluded from the SET list; the
                # placeholder count must be derived from the value tuple, not
                # hand-written, or a schema change quietly breaks every update.
                names = [name.strip() for name in COLUMNS.split(",")]
                assignments = [f"{name} = ?" for name in names if name != "id"]
                values = _row_values(rebased)[1:]
                self._conn.execute(
                    f"UPDATE tickets SET {', '.join(assignments)} WHERE id = ? AND version = ?",
                    (*values, rebased.id, current_version),
                )
                self._conn.commit()
                return rebased
            except Exception:
                self._conn.rollback()
                raise

    def all_tickets(self) -> Sequence[Ticket]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {COLUMNS} FROM tickets").fetchall()
        return [_to_ticket(row) for row in rows]

    def query(self, query: TicketQuery) -> Page:
        field, descending = sort_of(query)
        filter_key = query.cache_key()
        # Filters are applied in Python so that the risk band (a derived,
        # policy-dependent value) cannot drift between the two store adapters.
        candidates = [ticket for ticket in self.all_tickets() if matches(ticket, query)]
        candidates.sort(key=lambda ticket: (sort_key(ticket, field), ticket.id), reverse=descending)
        total = len(candidates)

        start = 0
        if query.cursor:
            payload = decode_cursor(query.cursor)
            if payload.get("f") not in (None, filter_key):
                from ...domain.errors import ValidationError

                raise ValidationError("cursor does not match the current filters; omit it to restart")
            target = (payload.get("k", ""), payload.get("i", ""))
            for index, ticket in enumerate(candidates):
                if (sort_key(ticket, field), ticket.id) == target:
                    start = index + 1
                    break
            else:
                start = total

        window = candidates[start : start + query.limit]
        has_more = start + query.limit < total
        next_cursor = None
        if has_more and window:
            tail = window[-1]
            next_cursor = encode_cursor(
                last_key=sort_key(tail, field),
                last_id=tail.id,
                direction="desc" if descending else "asc",
                filter_key=filter_key,
            )
        return Page(items=window, next_cursor=next_cursor, total=total, has_more=has_more)

    # -- audit / idempotency ---------------------------------------------
    def next_audit_seq(self, ticket_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS last FROM audit_log WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
        return int(row["last"]) + 1

    def append_audit(self, entry: AuditEntry) -> AuditEntry:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log (ticket_id, seq, action, actor, at, changes, meta) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.ticket_id,
                    entry.seq,
                    entry.action,
                    entry.actor,
                    entry.at.isoformat(),
                    json.dumps(entry.changes, ensure_ascii=False, default=str),
                    json.dumps(entry.meta, ensure_ascii=False, default=str),
                ),
            )
            self._conn.commit()
        return entry

    def audit_trail(self, ticket_id: str, *, limit: int = 200) -> Sequence[AuditEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ticket_id, seq, action, actor, at, changes, meta FROM audit_log "
                "WHERE ticket_id = ? ORDER BY seq ASC LIMIT ?",
                (ticket_id, limit),
            ).fetchall()
        return [
            AuditEntry(
                seq=int(row["seq"]),
                ticket_id=row["ticket_id"],
                action=row["action"],
                actor=row["actor"],
                at=datetime.fromisoformat(row["at"]),
                changes=json.loads(row["changes"]),
                meta=json.loads(row["meta"]),
            )
            for row in rows
        ]

    def find_idempotent(self, key: str) -> IdempotencyRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, ticket_id, request_fingerprint, status, body, created_at "
                "FROM idempotency_keys WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return IdempotencyRecord(
            key=row["key"],
            ticket_id=row["ticket_id"],
            request_fingerprint=row["request_fingerprint"],
            status=int(row["status"]),
            body=json.loads(row["body"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def remember_idempotent(self, record: IdempotencyRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO idempotency_keys "
                "(key, ticket_id, request_fingerprint, status, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.key,
                    record.ticket_id,
                    record.request_fingerprint,
                    record.status,
                    json.dumps(record.body, ensure_ascii=False, default=str),
                    record.created_at.isoformat(),
                ),
            )
            self._conn.commit()

    # -- extras -----------------------------------------------------------
    def size_bytes(self) -> int:
        path = Path(self.path)
        total = path.stat().st_size if path.exists() else 0
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.path}{suffix}")
            if sidecar.exists():
                total += sidecar.stat().st_size
        return total

    def table_counts(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for table in ("tickets", "audit_log", "idempotency_keys"):
                row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                out[table] = int(row["n"])
            return out


def _row_values(ticket: Ticket) -> tuple[Any, ...]:
    return (
        ticket.id,
        ticket.title,
        ticket.body,
        ticket.status.value,
        ticket.priority.value,
        ticket.team,
        ticket.reporter,
        ticket.assignee,
        ticket.channel,
        json.dumps(list(ticket.tags), ensure_ascii=False),
        json.dumps(dict(ticket.labels), ensure_ascii=False, default=str),
        ticket.created_at.isoformat(),
        ticket.updated_at.isoformat(),
        ticket.closed_at.isoformat() if ticket.closed_at else None,
        ticket.sla_due_at.isoformat() if ticket.sla_due_at else None,
        ticket.sla_duration_hours,
        ticket.sla_consumed_hours,
        ticket.sla_snooze_hours,
        ticket.sla_calendar,
        ticket.escalation_at.isoformat() if ticket.escalation_at else None,
        ticket.escalation_target,
        ticket.policy_hash,
        ticket.required_review,
        ticket.version,
    )


def _to_ticket(row: sqlite3.Row) -> Ticket:
    return Ticket.from_mapping(
        {
            "id": row["id"],
            "title": row["title"],
            "body": row["body"],
            "status": row["status"],
            "priority": row["priority"],
            "team": row["team"],
            "reporter": row["reporter"],
            "assignee": row["assignee"],
            "channel": row["channel"],
            "tags": json.loads(row["tags"] or "[]"),
            "labels": json.loads(row["labels"] or "{}"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "closed_at": row["closed_at"],
            "sla_due_at": row["sla_due_at"],
            "sla_duration_hours": row["sla_duration_hours"],
            "sla_consumed_hours": row["sla_consumed_hours"],
            "sla_snooze_hours": row["sla_snooze_hours"],
            "sla_calendar": row["sla_calendar"],
            "escalation_at": row["escalation_at"],
            "escalation_target": row["escalation_target"],
            "policy_hash": row["policy_hash"],
            "required_review": row["required_review"],
            "version": row["version"],
        }
    )


def utc_iso(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).isoformat()
