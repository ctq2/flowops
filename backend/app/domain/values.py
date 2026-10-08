"""Value objects and the ticket status machine."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Priority(str, enum.Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"

    @property
    def rank(self) -> int:
        return _PRIORITY_RANK[self]

    @property
    def label(self) -> str:
        return _PRIORITY_LABEL[self]

    @classmethod
    def parse(cls, value: object, default: "Priority | None" = None) -> "Priority":
        if isinstance(value, Priority):
            return value
        text = str(value or "").strip().upper()
        for item in cls:
            if item.value == text:
                return item
        if default is not None:
            return default
        raise ValueError(f"unknown priority {value!r}; expected one of {[p.value for p in cls]}")


_PRIORITY_RANK = {Priority.P0: 0, Priority.P1: 1, Priority.P2: 2, Priority.P3: 3}
_PRIORITY_LABEL = {
    Priority.P0: "Critical / 一级故障",
    Priority.P1: "High / 重要",
    Priority.P2: "Normal / 常规",
    Priority.P3: "Low / 咨询",
}


class Status(str, enum.Enum):
    OPEN = "open"
    TRIAGED = "triaged"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    RESOLVED = "resolved"
    CLOSED = "closed"

    @property
    def is_terminal(self) -> bool:
        return self in (Status.RESOLVED, Status.CLOSED)

    @property
    def is_active(self) -> bool:
        return not self.is_terminal

    @property
    def label(self) -> str:
        return _STATUS_LABEL[self]

    @classmethod
    def parse(cls, value: object) -> "Status":
        if isinstance(value, Status):
            return value
        text = str(value or "").strip().lower()
        for item in cls:
            if item.value == text:
                return item
        raise ValueError(f"unknown status {value!r}; expected one of {[s.value for s in cls]}")


_STATUS_LABEL = {
    Status.OPEN: "待分诊",
    Status.TRIAGED: "已分诊",
    Status.IN_PROGRESS: "处理中",
    Status.BLOCKED: "阻塞",
    Status.RESOLVED: "已解决",
    Status.CLOSED: "已关闭",
}

#: The workflow, encoded as data so the API, the console and the tests agree.
#:
#: Two invariants worth stating out loud:
#:
#: * **Nothing closes without being resolved.**  An operator who cannot mark
#:   unresolved work as done at 18:00 on a Friday is the whole reason SLA
#:   reporting can be trusted.
#: * **A mistyped ticket is discarded from OPEN, not from TRIAGED.**  Once someone
#:   has spent triage effort on it, the record must say what was concluded.
TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.OPEN: frozenset({Status.TRIAGED, Status.IN_PROGRESS, Status.BLOCKED, Status.CLOSED}),
    Status.TRIAGED: frozenset({Status.IN_PROGRESS, Status.BLOCKED, Status.OPEN}),
    Status.IN_PROGRESS: frozenset({Status.BLOCKED, Status.RESOLVED, Status.TRIAGED}),
    Status.BLOCKED: frozenset({Status.IN_PROGRESS, Status.TRIAGED, Status.RESOLVED}),
    Status.RESOLVED: frozenset({Status.CLOSED, Status.IN_PROGRESS}),  # reopen on regression
    Status.CLOSED: frozenset({Status.IN_PROGRESS}),  # reopen with an audit trail
}


def allowed_transitions(status: Status) -> frozenset[Status]:
    return TRANSITIONS[status]


def can_transition(source: Status, target: Status) -> bool:
    return target in TRANSITIONS[source]


class RiskBand(str, enum.Enum):
    """Coarse SLA health used by the console and by alerting."""

    BREACHED = "breached"
    CRITICAL = "critical"
    WARNING = "warning"
    HEALTHY = "healthy"
    UNTRACKED = "untracked"


#: Band thresholds as a fraction of the SLA budget already consumed.
RISK_THRESHOLDS: tuple[tuple[float, RiskBand], ...] = (
    (1.0, RiskBand.BREACHED),
    (0.85, RiskBand.CRITICAL),
    (0.6, RiskBand.WARNING),
    (0.0, RiskBand.HEALTHY),
)


def band_for(consumed_ratio: float | None, *, terminal: bool = False) -> RiskBand:
    if consumed_ratio is None:
        return RiskBand.UNTRACKED
    if terminal:
        # A closed ticket can still be reported as breached: that is the metric
        # customers argue about, so we never hide it.
        return RiskBand.BREACHED if consumed_ratio > 1.0 else RiskBand.HEALTHY
    for threshold, band in RISK_THRESHOLDS:
        if consumed_ratio >= threshold:
            return band
    return RiskBand.UNTRACKED  # pragma: no cover - the 0.0 row always matches


@dataclass(frozen=True, slots=True)
class SlaState:
    duration_hours: float
    consumed_hours: float
    snooze_hours: float = 0.0
    calendar: str = "business"

    @property
    def effective_hours(self) -> float:
        return max(0.0, self.duration_hours - self.snooze_hours)

    @property
    def remaining_hours(self) -> float:
        return self.effective_hours - self.consumed_hours

    @property
    def consumed_ratio(self) -> float:
        if self.effective_hours <= 0:
            return 1.0
        return self.consumed_hours / self.effective_hours
