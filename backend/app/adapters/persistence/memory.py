"""In-memory store.

Used by the test suite and by ``--memory`` runs of the API.  It implements the
same contract as the SQLite adapter, including cursor pagination, so a test that
passes here is meaningful for the real store.
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
from dataclasses import replace
from datetime import datetime
from typing import Any, Sequence

from ...domain.errors import ConflictError, NotFoundError, PreconditionFailedError, ValidationError
from ...domain.ticket import Ticket
from ...domain.values import Priority, RiskBand, Status
from ...application.models import AuditEntry, IdempotencyRecord, Page, TicketQuery

SORT_FIELDS = ("created_at", "updated_at", "priority", "sla_due_at")


def encode_cursor(*, last_key: str, last_id: str, direction: str, filter_key: str) -> str:
    payload = json.dumps(
        {"k": last_key, "i": last_id, "d": direction, "f": filter_key},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> dict[str, str]:
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
        payload = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationError("cursor is not valid; omit it to restart from the first page") from exc
    if not isinstance(payload, dict) or "k" not in payload or "i" not in payload:
        raise ValidationError("cursor is malformed; omit it to restart from the first page")
    return {str(k): str(v) for k, v in payload.items()}


def sort_of(query: TicketQuery) -> tuple[str, bool]:
    """Return ``(field, descending)`` for a query, validating the sort token."""
    raw = (query.sort or "-created_at").strip()
    descending = raw.startswith("-")
    field = raw.lstrip("+-")
    if field not in SORT_FIELDS:
        raise ValidationError(f"sort must be one of {', '.join(SORT_FIELDS)} (optionally prefixed with '-')")
    return field, descending


def sort_key(ticket: Ticket, field: str) -> str:
    if field == "priority":
        return f"{ticket.priority.rank:02d}"
    value = getattr(ticket, field, None)
    if isinstance(value, datetime):
        return value.isoformat()
    return "" if value is None else str(value)


def matches(ticket: Ticket, query: TicketQuery) -> bool:
    if query.status and ticket.status.value not in set(query.status):
        return False
    if query.priority and ticket.priority.value not in set(query.priority):
        return False
    if query.team and (ticket.team or "") != query.team:
        return False
    if query.risk and ticket.risk().value not in set(query.risk):
        return False
    if query.search:
        needle = query.search.strip().lower()
        haystack = f"{ticket.id} {ticket.title} {ticket.body} {ticket.reporter} {' '.join(ticket.tags)}".lower()
        if needle not in haystack:
            return False
    return True


def order_key(ticket: Ticket, field: str) -> tuple[str, str]:
    return (sort_key(ticket, field), ticket.id)


class InMemoryStore:
    """Thread-safe store with the same semantics as the SQLite adapter."""

    def __init__(self, tickets: Sequence[Ticket] | None = None) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._audit: dict[str, list[AuditEntry]] = {}
        self._idempotency: dict[str, IdempotencyRecord] = {}
        self._lock = threading.RLock()
        for ticket in tickets or ():
            self._tickets[ticket.id] = ticket

    # -- TicketStore ------------------------------------------------------
    def add(self, ticket: Ticket) -> Ticket:
        with self._lock:
            if ticket.id in self._tickets:
                raise ConflictError(f"ticket {ticket.id} already exists")
            self._tickets[ticket.id] = ticket
            return ticket

    def get(self, ticket_id: str) -> Ticket | None:
        with self._lock:
            return self._tickets.get(ticket_id)

    def save(self, ticket: Ticket, *, expected_version: int | None = None) -> Ticket:
        with self._lock:
            current = self._tickets.get(ticket.id)
            if current is None:
                raise NotFoundError(f"ticket {ticket.id} does not exist")
            if expected_version is not None and current.version != expected_version:
                raise PreconditionFailedError(
                    "the ticket changed since you loaded it",
                    expected_version=expected_version,
                    current_version=current.version,
                )
            # The store owns version monotonicity.  The service builds the new
            # state with a provisional version; whatever it guessed, the stored
            # version advances by exactly one so concurrent writers cannot skip
            # a number and confuse optimistic concurrency.
            stored = replace(ticket, version=current.version + 1)
            self._tickets[ticket.id] = stored
            return stored

    def all_tickets(self) -> Sequence[Ticket]:
        with self._lock:
            return list(self._tickets.values())

    def query(self, query: TicketQuery) -> Page:
        field, descending = sort_of(query)
        filter_key = query.cache_key()
        with self._lock:
            candidates = [ticket for ticket in self._tickets.values() if matches(ticket, query)]
        candidates.sort(key=lambda ticket: order_key(ticket, field), reverse=descending)
        total = len(candidates)

        start = 0
        if query.cursor:
            payload = decode_cursor(query.cursor)
            if payload.get("f") not in (None, filter_key):
                raise ValidationError("cursor does not match the current filters; omit it to restart")
            last_key = payload.get("k", "")
            last_id = payload.get("i", "")
            for index, ticket in enumerate(candidates):
                if order_key(ticket, field) == (last_key, last_id):
                    start = index + 1
                    break
            else:
                start = total  # cursor points past the end

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
            return len(self._audit.get(ticket_id, ())) + 1

    def append_audit(self, entry: AuditEntry) -> AuditEntry:
        with self._lock:
            self._audit.setdefault(entry.ticket_id, []).append(entry)
            return entry

    def audit_trail(self, ticket_id: str, *, limit: int = 200) -> Sequence[AuditEntry]:
        with self._lock:
            entries = list(self._audit.get(ticket_id, ()))
        return entries[-limit:]

    def find_idempotent(self, key: str) -> IdempotencyRecord | None:
        with self._lock:
            return self._idempotency.get(key)

    def remember_idempotent(self, record: IdempotencyRecord) -> None:
        with self._lock:
            self._idempotency.setdefault(record.key, record)

    # -- test helpers -----------------------------------------------------
    def counts(self) -> dict[str, int]:
        with self._lock:
            histogram: dict[str, int] = {status.value: 0 for status in Status}
            for ticket in self._tickets.values():
                histogram[ticket.status.value] += 1
            return histogram

    def risk_histogram(self) -> dict[str, int]:
        with self._lock:
            histogram: dict[str, int] = {band.value: 0 for band in RiskBand}
            for ticket in self._tickets.values():
                histogram[ticket.risk().value] += 1
            return histogram

    def priorities(self) -> dict[str, int]:
        with self._lock:
            histogram: dict[str, int] = {priority.value: 0 for priority in Priority}
            for ticket in self._tickets.values():
                histogram[ticket.priority.value] += 1
            return histogram

    def raw(self) -> dict[str, Any]:
        with self._lock:
            return {ticket_id: ticket.summary() for ticket_id, ticket in self._tickets.items()}
