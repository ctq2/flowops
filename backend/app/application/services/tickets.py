"""Ticket use cases.

Everything the API can do to a ticket is expressed here as a method that takes
plain data and returns domain objects.  No HTTP, no JSON, no SQL: that keeps the
use cases directly testable and lets the transport layer change independently.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ...domain.errors import ConflictError, NotFoundError, PreconditionFailedError, ValidationError
from ...domain.rules.engine import CompiledPolicy, Decision, Engine
from ...domain.ticket import Ticket
from ...domain.values import Priority, RiskBand, Status
from ..models import AuditEntry, IdempotencyRecord, Page, TicketQuery
from ..ports.store import Clock, EventSink, IdFactory, TicketStore

#: Fields a client may change directly through PATCH.
PATCHABLE = frozenset({"title", "body", "assignee", "team", "tags", "labels", "priority", "sla_snooze_hours"})

#: Teams the UI offers in a picker; anything else is accepted but flagged.
KNOWN_TEAMS = frozenset(
    {
        "tier0-incident",
        "platform",
        "app-engineering",
        "payments",
        "security",
        "key-accounts",
        "service-desk",
        "general",
    }
)

#: The complete field surface a policy may read.  Previewing against a partial
#: draft would make legitimate rules look broken, so the sandbox fills these in.
EMPTY_TICKET_FIELDS: dict[str, Any] = {
    "id": "TCK-PREVIEW",
    "title": "",
    "body": "",
    "status": Status.OPEN.value,
    "priority": Priority.P3.value,
    "team": None,
    "reporter": "preview",
    "assignee": None,
    "channel": "web",
    "tags": [],
    "labels": {},
    "sla_duration_hours": None,
    "sla_consumed_hours": None,
    "sla_snooze_hours": 0.0,
    "sla_calendar": "business",
    "escalation_target": None,
    "required_review": None,
}


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class UuidIdFactory:
    def ticket_id(self) -> str:
        return f"TCK-{uuid.uuid4().hex[:10].upper()}"


class NullEventSink:
    def emit(self, name: str, payload: Mapping[str, Any]) -> None:  # pragma: no cover - default
        return None


class ListEventSink:
    """Collects events in memory — used by tests and by the CLI demo."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, name: str, payload: Mapping[str, Any]) -> None:
        self.events.append((name, dict(payload)))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


class TicketService:
    def __init__(
        self,
        store: TicketStore,
        policy: CompiledPolicy,
        *,
        clock: Clock | None = None,
        ids: IdFactory | None = None,
        events: EventSink | None = None,
        engine_budget: int = 100_000,
    ) -> None:
        self.store = store
        self.policy = policy
        self.clock = clock or SystemClock()
        self.ids = ids or UuidIdFactory()
        self.events = events or NullEventSink()
        self.engine = Engine(policy, budget=engine_budget)

    # -- reads -----------------------------------------------------------
    def get(self, ticket_id: str) -> Ticket:
        ticket = self.store.get(ticket_id)
        if ticket is None:
            raise NotFoundError(f"ticket {ticket_id} does not exist")
        return ticket

    def trail(self, ticket_id: str, *, limit: int = 200) -> Sequence[AuditEntry]:
        self.get(ticket_id)
        return self.store.audit_trail(ticket_id, limit=limit)

    def list(self, query: TicketQuery) -> Page:
        if query.limit < 1 or query.limit > 200:
            raise ValidationError("limit must be between 1 and 200")
        return self.store.query(query)

    def sla_snapshot(self, *, now: datetime | None = None, horizon_hours: float = 4.0) -> dict[str, Any]:
        """Everything an operator needs to decide what to work on next."""
        moment = self._moment(now)
        tickets = self.store.all_tickets()
        open_tickets = [t for t in tickets if not t.is_terminal]
        due_soon = [
            t
            for t in open_tickets
            if t.remaining_hours() is not None and 0 <= (t.remaining_hours() or 0) <= horizon_hours
        ]
        return {
            "evaluated_at": moment.isoformat(),
            "horizon_hours": horizon_hours,
            "totals": {
                "all": len(tickets),
                "open": len(open_tickets),
                "breached": sum(1 for t in tickets if t.is_breached(moment)),
                "due_soon": len(due_soon),
                "escalation_due": sum(1 for t in open_tickets if t.escalation_due(moment)),
                "unassigned": sum(1 for t in open_tickets if not t.assignee),
            },
            "policy": {"name": self.policy.name, "version": self.policy.version, "hash": self.policy.source_hash},
            "calendar": self.policy.calendar.describe(),
        }

    # -- writes ----------------------------------------------------------
    def preview(self, ticket_data: Mapping[str, Any], *, now: datetime | None = None) -> Decision:
        """Dry-run the policy: no persistence, full trace.  Powers the UI sandbox.

        The draft is completed from :data:`EMPTY_TICKET_FIELDS` first.  A rule
        may legitimately read ``ticket.body`` or ``ticket.tags``, and a preview
        that omits those fields would fail with a field error that says nothing
        about the policy — an author would chase a phantom bug.
        """
        moment = self._moment(now)
        draft = {**EMPTY_TICKET_FIELDS, **dict(ticket_data)}
        draft.setdefault("created_at", moment.isoformat())
        draft["status"] = str(draft.get("status") or Status.OPEN.value)
        draft["priority"] = Priority.parse(draft.get("priority") or Priority.P3).value
        draft["tags"] = list(draft.get("tags") or [])
        draft["labels"] = dict(draft.get("labels") or {})
        return self.engine.evaluate(draft, now=moment, include_scope=True)

    def create(
        self,
        payload: Mapping[str, Any],
        *,
        actor: str = "api",
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
        triage: bool = True,
        now: datetime | None = None,
    ) -> tuple[Ticket, Decision | None, bool]:
        """Create a ticket.  Returns ``(ticket, decision, replayed)``."""
        # Normalise to UTC once, up front: SLA arithmetic mixes the clock with
        # client-supplied timestamps, and comparing different offsets by
        # subtraction silently shifts every measurement.
        moment = self._moment(now)
        fingerprint = request_fingerprint or _fingerprint(payload)

        if idempotency_key:
            existing = self.store.find_idempotent(idempotency_key)
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise ConflictError(
                        "this Idempotency-Key was already used with a different request body",
                        key=idempotency_key,
                    )
                replayed = self.get(existing.ticket_id)
                return replayed, None, True

        ticket = Ticket.create(
            title=payload.get("title", ""),
            body=payload.get("body", ""),
            reporter=payload.get("reporter") or "anonymous",
            channel=payload.get("channel") or "web",
            priority=payload.get("priority") or Priority.P3,
            team=payload.get("team"),
            tags=tuple(payload.get("tags") or ()),
            labels=payload.get("labels") or {},
            created_at=_coerce_moment(payload.get("created_at"), moment),
            ticket_id=payload.get("id") or self.ids.ticket_id(),
        )

        decision: Decision | None = None
        if triage:
            ticket, decision = self._triage(ticket, moment)

        stored = self.store.add(ticket)
        self._audit(
            stored.id,
            action="ticket.created",
            actor=actor,
            at=moment,
            changes={"title": stored.title, "channel": stored.channel, "reporter": stored.reporter},
            meta={
                "policy": {"name": self.policy.name, "version": self.policy.version, "hash": self.policy.source_hash},
                "rules_fired": list(decision.fired) if decision else [],
                "sla_due_at": stored.sla_due_at.isoformat() if stored.sla_due_at else None,
            },
        )
        if decision:
            self._audit(
                stored.id,
                action="policy.evaluated",
                actor="policy-engine",
                at=moment,
                changes={"priority": stored.priority.value, "team": stored.team},
                meta={
                    "rules_fired": list(decision.fired),
                    "trace": [entry.as_dict() for entry in decision.trace],
                    "notifications": [item.as_dict() for item in decision.notifications],
                    "policy_hash": decision.policy_hash,
                },
            )

        if idempotency_key:
            self.store.remember_idempotent(
                IdempotencyRecord(
                    key=idempotency_key,
                    ticket_id=stored.id,
                    request_fingerprint=fingerprint,
                    status=201,
                    body=stored.summary(moment),
                    created_at=moment,
                )
            )
        self.events.emit("ticket.created", {"ticket_id": stored.id, "priority": stored.priority.value})
        return stored, decision, False

    def update(
        self,
        ticket_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str = "api",
        expected_version: int | None = None,
        now: datetime | None = None,
    ) -> Ticket:
        moment = self._moment(now)
        ticket = self.get(ticket_id)
        unknown = set(payload) - PATCHABLE
        if unknown:
            raise ValidationError(f"fields cannot be patched: {', '.join(sorted(unknown))}")

        changes: dict[str, Any] = {}
        updated = ticket
        for field_name, raw in payload.items():
            if field_name == "priority":
                try:
                    value: Any = Priority.parse(raw)
                except ValueError as exc:
                    raise ValidationError(str(exc)) from exc
            elif field_name == "tags":
                if not isinstance(raw, (list, tuple)):
                    raise ValidationError("tags must be an array")
                value = tuple(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
            elif field_name == "labels":
                if not isinstance(raw, Mapping):
                    raise ValidationError("labels must be an object")
                value = dict(raw)
            elif field_name == "sla_snooze_hours":
                try:
                    value = float(raw)
                except (TypeError, ValueError) as exc:
                    raise ValidationError("sla_snooze_hours must be a number") from exc
                if value < 0:
                    raise ValidationError("sla_snooze_hours cannot be negative")
            elif field_name == "title":
                value = str(raw).strip()
                if not value or len(value) > 160:
                    raise ValidationError("title must be 1..160 characters")
            else:
                value = raw
            if getattr(updated, field_name) != value:
                changes[field_name] = value
            # Keep the provisional version stable while assembling the new state;
            # the store assigns the real one so concurrent writers cannot race.
            updated = updated.with_updates(version=updated.version, **{field_name: value})

        if not changes:
            return ticket

        if "sla_snooze_hours" in changes:
            refreshed = self._resolve_sla(updated, moment)
            for name in ("sla_due_at", "sla_consumed_hours", "sla_snooze_hours", "escalation_at"):
                new_value = getattr(refreshed, name, None)
                if getattr(ticket, name, None) != new_value:
                    changes[name] = new_value
            updated = refreshed

        if expected_version is not None and ticket.version != expected_version:
            raise PreconditionFailedError(
                "the ticket changed since you loaded it",
                expected_version=expected_version,
                current_version=ticket.version,
            )

        stored = self.store.save(updated, expected_version=expected_version)
        self._audit(
            stored.id,
            action="ticket.updated",
            actor=actor,
            at=moment,
            changes=_serialisable(changes),
            meta={"version": stored.version},
        )
        self.events.emit("ticket.updated", {"ticket_id": stored.id, "fields": sorted(changes)})
        return stored

    def transition(
        self,
        ticket_id: str,
        target: str,
        *,
        actor: str = "api",
        note: str | None = None,
        expected_version: int | None = None,
        now: datetime | None = None,
    ) -> Ticket:
        moment = self._moment(now)
        ticket = self.get(ticket_id)
        try:
            status = Status.parse(target)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        ticket.assert_can_transition(status)

        if expected_version is not None and ticket.version != expected_version:
            raise PreconditionFailedError(
                "the ticket changed since you loaded it",
                expected_version=expected_version,
                current_version=ticket.version,
            )

        changes: dict[str, Any] = {"status": status.value}
        updates: dict[str, Any] = {"status": status}
        if status in (Status.RESOLVED, Status.CLOSED):
            updates["closed_at"] = ticket.closed_at or moment
        elif ticket.closed_at is not None:
            updates["closed_at"] = None  # reopened: the SLA clock starts again

        updated = ticket.with_updates(**updates)
        if updated.sla_duration_hours is not None:
            updated = self._resolve_sla(updated, moment)

        stored = self.store.save(updated, expected_version=expected_version)
        self._audit(
            stored.id,
            action="ticket.transitioned",
            actor=actor,
            at=moment,
            changes={**changes, "from": ticket.status.value},
            meta={"note": note, "version": stored.version, "closed_at": changes.get("closed_at")},
        )
        self.events.emit(
            "ticket.transitioned",
            {"ticket_id": stored.id, "from": ticket.status.value, "to": status.value},
        )
        return stored

    def refresh_sla(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Recompute SLA clocks for every active ticket.

        This is the sweep that makes holiday-calendar corrections and snooze
        changes visible without a write from a human.
        """
        moment = self._moment(now)
        touched = 0
        breached = 0
        for ticket in self.store.all_tickets():
            if ticket.sla_duration_hours is None:
                continue
            refreshed = self._resolve_sla(ticket, moment)
            if refreshed.sla_due_at != ticket.sla_due_at or refreshed.sla_consumed_hours != ticket.sla_consumed_hours:
                touched += 1
                stored = self.store.save(refreshed)
                if stored.is_breached(moment):
                    breached += 1
        return {"evaluated_at": moment.isoformat(), "tickets_touched": touched, "breached": breached}

    # -- internals -------------------------------------------------------
    def _moment(self, now: datetime | None) -> datetime:
        """Every instant the service handles is a UTC instant.

        Mixing offsets — a client sends ``+08:00`` while the clock returns UTC —
        and then *subtracting* produces a plausible-looking number that is off by
        the offset.  That is the bug that surfaces three weeks later as "our SLA
        figures are wrong by two hours".  Normalising once, here, removes the
        whole class of problem.
        """
        moment = now or self.clock.now()
        if moment.tzinfo is None:
            raise ValidationError("timestamps must include a timezone offset")
        return moment.astimezone(timezone.utc)

    def _triage(self, ticket: Ticket, moment: datetime) -> tuple[Ticket, Decision]:
        decision = self.engine.evaluate(ticket.to_mapping(), now=moment, include_scope=False)
        fields = decision.fields
        updates: dict[str, Any] = {
            "priority": Priority.parse(fields.get("priority") or ticket.priority),
            "team": fields.get("team") or ticket.team,
            "sla_duration_hours": fields.get("sla_duration_hours"),
            "sla_consumed_hours": fields.get("sla_consumed_hours"),
            "sla_snooze_hours": float(fields.get("sla_snooze_hours") or 0.0),
            "sla_calendar": str(fields.get("sla_calendar") or "business"),
            "sla_due_at": fields.get("sla_due_at"),
            "escalation_at": fields.get("escalation_at"),
            "escalation_target": fields.get("escalation_target"),
            "policy_hash": decision.policy_hash,
            "required_review": decision.required_review,
        }
        status = Status.TRIAGED if decision.fired else Status.OPEN
        updates["status"] = status
        # Persisting is what turns the triaged draft into version 1 of the ticket.
        return ticket.with_updates(version=1, **updates), decision

    def _resolve_sla(self, ticket: Ticket, moment: datetime) -> Ticket:
        """Re-derive SLA *timestamps* while keeping the frozen budget.

        Only the clock side is recomputed (a holiday may have been added, the
        calendar may have changed).  Priority, team and the duration itself stay
        exactly as they were decided when the ticket was triaged — changing them
        retroactively would rewrite history and invalidate the audit trail.
        """
        decision = self.engine.evaluate(
            ticket.to_mapping(), now=moment, duration=ticket.sla_duration_hours
        )
        fields = decision.fields
        return ticket.with_updates(
            sla_duration_hours=fields.get("sla_duration_hours", ticket.sla_duration_hours),
            sla_consumed_hours=fields.get("sla_consumed_hours"),
            sla_snooze_hours=float(fields.get("sla_snooze_hours") or ticket.sla_snooze_hours),
            sla_calendar=str(fields.get("sla_calendar") or ticket.sla_calendar),
            sla_due_at=fields.get("sla_due_at"),
            escalation_at=fields.get("escalation_at"),
        )

    def _audit(
        self,
        ticket_id: str,
        *,
        action: str,
        actor: str,
        at: datetime,
        changes: Mapping[str, Any] | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            seq=self.store.next_audit_seq(ticket_id),
            ticket_id=ticket_id,
            action=action,
            actor=actor,
            at=at,
            changes=dict(changes or {}),
            meta=dict(meta or {}),
        )
        return self.store.append_audit(entry)

    def risk_histogram(self, *, now: datetime | None = None) -> dict[str, int]:
        moment = self._moment(now)
        histogram = {band.value: 0 for band in RiskBand}
        for ticket in self.store.all_tickets():
            histogram[ticket.risk().value] += 1
        return histogram


def _coerce_moment(value: Any, fallback: datetime) -> datetime:
    """Parse a client timestamp, always returning a UTC instant."""
    if value is None:
        return fallback.astimezone(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValidationError("created_at must include a timezone offset")
        return value.astimezone(timezone.utc)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"created_at: {value!r} is not an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValidationError("created_at must include a timezone offset")
        return parsed.astimezone(timezone.utc)
    raise ValidationError("created_at must be an ISO-8601 timestamp")


def _fingerprint(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _serialisable(changes: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in changes.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif isinstance(value, (Status, Priority)):
            out[key] = value.value
        elif isinstance(value, tuple):
            out[key] = list(value)
        else:
            out[key] = value
    return out
