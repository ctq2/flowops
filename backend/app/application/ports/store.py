"""Ports: the interfaces the application layer owns and adapters implement.

Dependency inversion lives here.  The application declares what it needs; the
persistence and transport adapters are free to change without touching a single
use case.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ...domain.ticket import Ticket
from ..models import AuditEntry, IdempotencyRecord, Page, TicketQuery


@runtime_checkable
class TicketStore(Protocol):
    """Persistence boundary for tickets, audit entries and idempotency keys."""

    def add(self, ticket: Ticket) -> Ticket: ...

    def get(self, ticket_id: str) -> Ticket | None: ...

    def save(self, ticket: Ticket, *, expected_version: int | None = None) -> Ticket: ...

    def query(self, query: TicketQuery) -> Page: ...

    def append_audit(self, entry: AuditEntry) -> AuditEntry: ...

    def audit_trail(self, ticket_id: str, *, limit: int = 200) -> Sequence[AuditEntry]: ...

    def next_audit_seq(self, ticket_id: str) -> int: ...

    def find_idempotent(self, key: str) -> IdempotencyRecord | None: ...

    def remember_idempotent(self, record: IdempotencyRecord) -> None: ...

    def all_tickets(self) -> Sequence[Ticket]: ...


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


@runtime_checkable
class IdFactory(Protocol):
    def ticket_id(self) -> str: ...


@runtime_checkable
class EventSink(Protocol):
    """Fan-out hook for observability (structured logs, metrics, outbox)."""

    def emit(self, name: str, payload: Mapping[str, Any]) -> None: ...
