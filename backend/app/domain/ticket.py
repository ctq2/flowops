"""The Ticket aggregate.

The aggregate is deliberately boring: a validated, immutable snapshot plus the
handful of domain questions the application layer needs answered (what may
happen next, how healthy is the SLA, what does the customer see).  All mutation
happens through ``evolve()`` which returns a new instance and bumps ``version``,
which the repository uses for optimistic concurrency.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .errors import IllegalTransitionError, ValidationError
from .values import Priority, RiskBand, SlaState, Status, allowed_transitions, band_for, can_transition

MAX_TITLE = 160
MAX_BODY = 8_000
MAX_TAGS = 12


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: Any, what: str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValidationError(f"{what} must include a timezone offset")
        return value.astimezone(timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{what}: {value!r} is not an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{what}: {value!r} has no timezone offset")
        return parsed.astimezone(timezone.utc)
    raise ValidationError(f"{what} must be an ISO-8601 timestamp")


@dataclass(frozen=True, slots=True)
class Ticket:
    id: str
    title: str
    body: str
    status: Status
    priority: Priority
    team: str | None
    reporter: str
    assignee: str | None
    channel: str
    tags: tuple[str, ...]
    labels: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    sla_due_at: datetime | None
    sla_duration_hours: float | None
    sla_consumed_hours: float | None
    sla_snooze_hours: float
    sla_calendar: str
    escalation_at: datetime | None
    escalation_target: str | None
    policy_hash: str | None
    required_review: str | None
    version: int
    history: tuple[Mapping[str, Any], ...] = field(default=())

    # -- factory ---------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        title: str,
        body: str = "",
        reporter: str = "anonymous",
        channel: str = "web",
        priority: Priority | str = Priority.P3,
        team: str | None = None,
        tags: tuple[str, ...] = (),
        labels: Mapping[str, Any] | None = None,
        created_at: datetime | None = None,
        ticket_id: str | None = None,
    ) -> "Ticket":
        title = (title or "").strip()
        if not title:
            raise ValidationError("title is required")
        if len(title) > MAX_TITLE:
            raise ValidationError(f"title must be at most {MAX_TITLE} characters")
        body = (body or "").strip()
        if len(body) > MAX_BODY:
            raise ValidationError(f"body must be at most {MAX_BODY} characters")
        cleaned_tags = tuple(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))
        if len(cleaned_tags) > MAX_TAGS:
            raise ValidationError(f"at most {MAX_TAGS} tags are allowed")
        moment = created_at or utcnow()
        return cls(
            id=ticket_id or f"TCK-{uuid.uuid4().hex[:10].upper()}",
            title=title,
            body=body,
            status=Status.OPEN,
            priority=Priority.parse(priority),
            team=team,
            reporter=reporter or "anonymous",
            assignee=None,
            channel=channel or "web",
            tags=cleaned_tags,
            labels=dict(labels or {}),
            created_at=moment,
            updated_at=moment,
            closed_at=None,
            sla_due_at=None,
            sla_duration_hours=None,
            sla_consumed_hours=None,
            sla_snooze_hours=0.0,
            sla_calendar="business",
            escalation_at=None,
            escalation_target=None,
            policy_hash=None,
            required_review=None,
            version=1,
            history=(),
        )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Ticket":
        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            body=str(data.get("body") or ""),
            status=Status.parse(data.get("status", "open")),
            priority=Priority.parse(data.get("priority", "P3")),
            team=data.get("team"),
            reporter=str(data.get("reporter") or "anonymous"),
            assignee=data.get("assignee"),
            channel=str(data.get("channel") or "web"),
            tags=tuple(data.get("tags") or ()),
            labels=dict(data.get("labels") or {}),
            created_at=parse_timestamp(data["created_at"], "created_at"),
            updated_at=parse_timestamp(data["updated_at"], "updated_at"),
            closed_at=parse_timestamp(data["closed_at"], "closed_at") if data.get("closed_at") else None,
            sla_due_at=parse_timestamp(data["sla_due_at"], "sla_due_at") if data.get("sla_due_at") else None,
            sla_duration_hours=_opt_float(data.get("sla_duration_hours")),
            sla_consumed_hours=_opt_float(data.get("sla_consumed_hours")),
            sla_snooze_hours=float(data.get("sla_snooze_hours") or 0.0),
            sla_calendar=str(data.get("sla_calendar") or "business"),
            escalation_at=parse_timestamp(data["escalation_at"], "escalation_at")
            if data.get("escalation_at")
            else None,
            escalation_target=data.get("escalation_target"),
            policy_hash=data.get("policy_hash"),
            required_review=data.get("required_review"),
            version=int(data.get("version") or 1),
            history=tuple(data.get("history") or ()),
        )

    # -- projection ------------------------------------------------------
    def to_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "status": self.status.value,
            "priority": self.priority.value,
            "team": self.team,
            "reporter": self.reporter,
            "assignee": self.assignee,
            "channel": self.channel,
            "tags": list(self.tags),
            "labels": dict(self.labels),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "sla_due_at": self.sla_due_at.isoformat() if self.sla_due_at else None,
            "sla_duration_hours": self.sla_duration_hours,
            "sla_consumed_hours": self.sla_consumed_hours,
            "sla_snooze_hours": self.sla_snooze_hours,
            "sla_calendar": self.sla_calendar,
            "escalation_at": self.escalation_at.isoformat() if self.escalation_at else None,
            "escalation_target": self.escalation_target,
            "policy_hash": self.policy_hash,
            "required_review": self.required_review,
            "version": self.version,
        }

    def with_updates(self, *, version: int | None = None, **changes: Any) -> "Ticket":
        """Return a copy with ``version`` bumped.

        The store owns version monotonicity, so callers pass the provisional
        version they expect; anything else here is a plain field replacement.
        """
        changes.setdefault("updated_at", utcnow())
        changes["version"] = self.version + 1 if version is None else version
        return replace(self, **changes)

    def append_history(self, entry: Mapping[str, Any]) -> "Ticket":
        return replace(self, history=(*self.history, dict(entry)))

    # -- domain questions ------------------------------------------------
    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def allowed_transitions(self) -> list[str]:
        return sorted(item.value for item in allowed_transitions(self.status))

    def assert_can_transition(self, target: Status) -> None:
        if not can_transition(self.status, target):
            raise IllegalTransitionError(
                f"cannot move {self.id} from {self.status.value} to {target.value}",
                allowed=self.allowed_transitions(),
            )

    def sla_state(self) -> SlaState | None:
        if self.sla_duration_hours is None or self.sla_consumed_hours is None:
            return None
        return SlaState(
            duration_hours=self.sla_duration_hours,
            consumed_hours=self.sla_consumed_hours,
            snooze_hours=self.sla_snooze_hours,
            calendar=self.sla_calendar,
        )

    def consumed_ratio(self) -> float | None:
        state = self.sla_state()
        return None if state is None else state.consumed_ratio

    def risk(self) -> RiskBand:
        return band_for(self.consumed_ratio(), terminal=self.is_terminal)

    def remaining_hours(self) -> float | None:
        state = self.sla_state()
        return None if state is None else state.remaining_hours

    def is_breached(self, now: datetime | None = None) -> bool:
        if self.sla_due_at is None:
            return False
        reference = self.closed_at or now or utcnow()
        return reference > self.sla_due_at

    def age_hours(self, now: datetime | None = None) -> float:
        reference = self.closed_at or now or utcnow()
        return round((reference - self.created_at).total_seconds() / 3600.0, 4)

    def escalation_due(self, now: datetime | None = None) -> bool:
        if self.escalation_at is None or self.is_terminal:
            return False
        return (now or utcnow()) >= self.escalation_at

    def summary(self, now: datetime | None = None) -> dict[str, Any]:
        """The shape the console consumes — includes derived, time-sensitive fields."""
        payload = self.to_mapping()
        payload.update(
            {
                "risk": self.risk().value,
                "consumed_ratio": round(self.consumed_ratio(), 4) if self.consumed_ratio() is not None else None,
                "remaining_hours": self.remaining_hours(),
                "age_hours": self.age_hours(now),
                "breached": self.is_breached(now),
                "escalation_due": self.escalation_due(now),
                "allowed_transitions": self.allowed_transitions(),
                "status_label": self.status.label,
                "priority_label": self.priority.label,
            }
        )
        return payload


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{value!r} is not a number") from exc


def overdue_by(ticket: Ticket, now: datetime | None = None) -> timedelta | None:
    if ticket.sla_due_at is None:
        return None
    reference = ticket.closed_at or now or utcnow()
    delta = reference - ticket.sla_due_at
    return delta if delta > timedelta(0) else None
