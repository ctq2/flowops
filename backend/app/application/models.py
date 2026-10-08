"""Application-level data structures (not domain entities, not wire formats)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One immutable line of the ticket audit trail.

    The trail is append-only and never rewritten: when the SLA policy changes
    six months from now, an auditor must still be able to see which rule fired
    for which ticket at which instant.
    """

    seq: int
    ticket_id: str
    action: str
    actor: str
    at: datetime
    changes: Mapping[str, Any] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ticket_id": self.ticket_id,
            "action": self.action,
            "actor": self.actor,
            "at": self.at.isoformat(),
            "changes": dict(self.changes),
            "meta": dict(self.meta),
        }


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """Result of a previously completed write, keyed by client-supplied token."""

    key: str
    ticket_id: str
    request_fingerprint: str
    status: int
    body: Mapping[str, Any]
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "ticket_id": self.ticket_id,
            "request_fingerprint": self.request_fingerprint,
            "status": self.status,
            "body": dict(self.body),
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class TicketQuery:
    """Filter + cursor description for a ticket listing."""

    status: Sequence[str] | None = None
    priority: Sequence[str] | None = None
    team: str | None = None
    risk: Sequence[str] | None = None
    search: str | None = None
    sort: str = "-created_at"
    limit: int = 50
    cursor: str | None = None

    def cache_key(self) -> str:
        """Stable representation used inside a cursor to detect filter changes."""
        return "|".join(
            [
                ",".join(sorted(self.status or ())),
                ",".join(sorted(self.priority or ())),
                self.team or "",
                ",".join(sorted(self.risk or ())),
                (self.search or "").lower(),
                self.sort,
            ]
        )


@dataclass(frozen=True, slots=True)
class Page:
    items: Sequence[Any]
    next_cursor: str | None
    total: int
    has_more: bool
