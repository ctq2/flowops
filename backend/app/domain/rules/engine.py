"""The policy engine.

A *policy document* is JSON authored by an operations lead.  It is compiled once
into ``CompiledPolicy`` (rules, parsed conditions, resolved actions) and then
replayed against tickets.  Three properties matter in production:

determinism
    ``evaluate(ticket, now)`` is a pure function of its inputs.  Replaying a
    ticket at a fixed instant always yields the same decision, which is what
    makes audit logs and incident reviews meaningful.
explainability
    Every decision carries a trace: which rule fired, which action it ran, what
    the value was before and after.  "Why is this P1 routed to payments?" must be
    answerable without reading code.
safety
    Author-supplied strings are data, never code paths.  Unknown actions,
    enum values and durations fail the whole policy load rather than the
    individual ticket, so a broken policy is caught at deploy time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..errors import RuleError, RuleEvaluationError, ValidationError
from .calendar import BusinessCalendar
from .evaluator import Evaluator, to_jsonable, truthy
from .functions import SIGNATURES, FunctionLibrary
from .parser import KNOWN_FUNCTIONS, parse
from .scope import Scope, build_scope

#: Every action the engine understands, with the arity it expects.
ACTION_CATALOG: dict[str, str] = {
    "set_priority": "set_priority('P1') or 'set_priority:P1' — one of P0..P3",
    "assign_team": "assign_team('payments') — route the ticket",
    "set_sla:duration": "{'action': 'set_sla:duration', 'value': 'business_hours_between(...)'} — service target in hours",
    "set_sla:calendar": "set_sla:calendar('business') — measure on working hours or wall-clock",
    "set_sla:snooze": "{'action': 'set_sla:snooze', 'value': 4} — hours of pause to subtract",
    "sla_recompute": "sla_recompute() — resolve the due timestamp from the draft",
    "escalate:after": "{'action': 'escalate:after', 'value': 0.5} — escalation delay in hours",
    "escalate:to": "escalate:to('oncall-sre') — escalation target",
    "escalate:notify": "escalate:notify('slack', '#ops') — record an outbound notification",
    "tag": "tag('vip', ...) — attach tags",
    "label": "{'action': 'label', 'value': ['key', 'value']} — set an arbitrary label",
    "require_review": "require_review('reason') — flag for a human gate",
    "note": "note('text') — append a trace note",
    "stop": "stop() — stop processing further rules",
}

CALENDAR_CHOICES = ("business", "wall")
STRATEGIES = ("first_match", "accumulate")


@dataclass(frozen=True, slots=True)
class Action:
    code: str
    args: tuple[Any, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "args": [to_jsonable(arg) for arg in self.args]}


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    when: str | None
    actions: tuple[Action, ...]
    stop: bool = False
    description: str = ""
    priority: int = 100
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Rule":
        if not isinstance(data, Mapping):
            raise ValidationError("each rule must be a JSON object")
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValidationError("every rule needs a non-empty 'name'")
        raw_actions = data.get("then") or data.get("actions") or []
        return cls(
            name=name,
            when=data.get("when"),
            actions=tuple(parse_action(item) for item in raw_actions),
            stop=bool(data.get("stop", False)),
            description=str(data.get("description") or ""),
            priority=int(data.get("priority", 100)),
            tags=tuple(str(t) for t in (data.get("tags") or ())),
        )


def parse_action(item: Any) -> Action:
    """Normalise one ``then`` entry.

    Two unambiguous forms are accepted.  Ambiguity is rejected rather than
    guessed at, because a mis-parsed action silently routes tickets to the wrong
    team:

    * ``{"action": "set_sla:duration", "value": 4}`` — any action, any arity.
      Also accepts ``args``/``with`` (a list) or a single extra key.
    * ``"assign_team:payments"`` — shorthand: the action name is everything up to
      the last colon, and the tail is the value, never re-split.  That keeps
      ``"escalate:to:oncall-sre"`` and ``"set_sla:calendar:business"`` readable.
      Actions that take more than one argument must use the object form.
    """
    if isinstance(item, str):
        text = item.strip()
        if not text:
            raise ValidationError("action entry is empty")
        # The action name is everything up to the last colon; the tail is the
        # value.  This keeps `escalate:to:oncall` and
        # `set_sla:calendar:business` readable without guessing: the tail is
        # never re-split, so a value that itself contains a colon stays whole.
        index = text.rfind(":")
        if index == -1:
            return Action(text, ())
        code = text[:index].strip()
        tail = text[index + 1 :].strip()
        if not code:
            raise ValidationError(f"action entry has no action name: {item!r}")
        if tail == "":
            raise ValidationError(
                f"{item!r} has a trailing colon but no value; write {code!r} alone or add the value"
            )
        return Action(code, (tail,))
    if isinstance(item, Mapping):
        name = str(item.get("action") or item.get("code") or "").strip()
        if not name:
            raise ValidationError("action object needs an 'action' key")
        for key in ("value", "args", "with"):
            if key in item:
                raw = item[key]
                if isinstance(raw, (list, tuple)):
                    return Action(name, tuple(raw))
                return Action(name, (raw,))
        rest = {k: v for k, v in item.items() if k not in ("action", "code")}
        if len(rest) == 1:
            return Action(name, (next(iter(rest.values())),))
        if not rest:
            return Action(name, ())
        return Action(name, (rest,))
    raise ValidationError(f"unsupported action entry: {item!r}")


@dataclass(frozen=True, slots=True)
class CompiledRule:
    rule: Rule
    condition: Any | None  # ast.Node


@dataclass(frozen=True, slots=True)
class CompiledPolicy:
    name: str
    version: str
    strategy: str
    rules: tuple[CompiledRule, ...]
    fallback: tuple[CompiledRule, ...]
    calendar: BusinessCalendar
    defaults: Mapping[str, Any]
    source_hash: str

    @property
    def all_rules(self) -> tuple[CompiledRule, ...]:
        return (*self.rules, *self.fallback)


@dataclass(frozen=True, slots=True)
class Policy:
    """Raw policy document plus the compiled form."""

    document: Mapping[str, Any]
    compiled: CompiledPolicy

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "Policy":
        return cls(document=document, compiled=compile_policy(document))

    @classmethod
    def from_json(cls, text: str) -> "Policy":
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"policy is not valid JSON: {exc}") from exc
        if not isinstance(document, Mapping):
            raise ValidationError("policy root must be a JSON object")
        return cls.from_dict(document)

    @classmethod
    def from_file(cls, path: str | Path) -> "Policy":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


def compile_policy(document: Mapping[str, Any]) -> CompiledPolicy:
    """Parse and validate a policy document.  Raises on the *first* problem."""
    if not isinstance(document, Mapping):
        raise ValidationError("policy must be a JSON object")
    name = str(document.get("name") or "unnamed-policy")
    version = str(document.get("version") or "0")
    strategy = str(document.get("strategy") or "first_match")
    if strategy not in STRATEGIES:
        raise ValidationError(f"strategy must be one of {STRATEGIES}, got {strategy!r}")

    raw_rules = document.get("rules")
    if not isinstance(raw_rules, Sequence) or isinstance(raw_rules, (str, bytes)) or not raw_rules:
        raise ValidationError("policy needs a non-empty 'rules' array")

    compiled: list[CompiledRule] = []
    seen: set[str] = set()
    for item in raw_rules:
        rule = Rule.from_dict(item)
        if rule.name in seen:
            raise ValidationError(f"duplicate rule name {rule.name!r}")
        seen.add(rule.name)
        if not rule.actions:
            raise ValidationError(f"rule {rule.name!r} has no actions")
        for action in rule.actions:
            validate_action(rule.name, action)
        compiled.append(CompiledRule(rule, parse(rule.when) if rule.when else None))

    fallback: list[CompiledRule] = []
    raw_fallback = document.get("fallback") or ()
    if isinstance(raw_fallback, Mapping):
        raw_fallback = [raw_fallback]
    for item in raw_fallback:
        rule = Rule.from_dict(item)
        for action in rule.actions:
            validate_action(rule.name, action)
        fallback.append(CompiledRule(rule, parse(rule.when) if rule.when else None))

    # `priority` orders execution (10 before 90); the file order breaks ties, so
    # a policy can be grouped by topic and still evaluated strictly.
    compiled.sort(key=lambda item: item.rule.priority)
    fallback.sort(key=lambda item: item.rule.priority)

    calendar = BusinessCalendar.from_dict(dict(document.get("calendar") or {}))
    defaults = dict(document.get("defaults") or {})
    digest_source = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return CompiledPolicy(
        name=name,
        version=version,
        strategy=strategy,
        rules=tuple(compiled),
        fallback=tuple(fallback),
        calendar=calendar,
        defaults=defaults,
        source_hash=_digest(digest_source),
    )


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def validate_action(rule_name: str, action: Action) -> None:
    """Reject anything the runtime would refuse to execute later."""
    if action.code not in ACTION_CATALOG:
        raise ValidationError(
            f"rule {rule_name!r}: unknown action {action.code!r}; known actions: "
            + ", ".join(sorted(ACTION_CATALOG))
        )
    expected = {
        "set_priority": (1, 1),
        "assign_team": (1, 1),
        "set_sla:duration": (1, 1),
        "set_sla:snooze": (1, 1),
        "set_sla:calendar": (1, 1),
        "sla_recompute": (0, 0),
        "escalate:after": (1, 1),
        "escalate:to": (1, 1),
        "escalate:notify": (1, 8),
        "tag": (1, 8),
        "label": (2, 2),
        "require_review": (0, 1),
        "note": (1, 1),
        "stop": (0, 0),
    }[action.code]
    low, high = expected
    if not low <= len(action.args) <= high:
        raise ValidationError(
            f"rule {rule_name!r}: {action.code} expects {low}"
            + (f"..{high}" if high != low else "")
            + f" argument(s), got {len(action.args)}"
        )
    if action.code == "set_priority":
        value = str(action.args[0]).upper()
        if value not in PRIORITY_CHOICES:
            raise ValidationError(
                f"rule {rule_name!r}: priority must be one of {PRIORITY_CHOICES}, got {action.args[0]!r}"
            )
    if action.code == "set_sla:calendar" and str(action.args[0]) not in CALENDAR_CHOICES:
        raise ValidationError(
            f"rule {rule_name!r}: calendar must be one of {CALENDAR_CHOICES}, got {action.args[0]!r}"
        )
    if action.code in ("set_sla:duration", "set_sla:snooze", "escalate:after"):
        # These accept *expressions*, so they are compiled with the policy.
        parse(str(action.args[0]))


PRIORITY_CHOICES = ("P0", "P1", "P2", "P3")


@dataclass(slots=True)
class TraceEntry:
    rule: str
    action: str
    before: Any = None
    after: Any = None
    note: str = ""
    ok: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "action": self.action,
            "before": to_jsonable(self.before),
            "after": to_jsonable(self.after),
            "note": self.note,
            "ok": self.ok,
        }


@dataclass(slots=True)
class Notification:
    channel: str
    payload: Any
    after_hours: float | None = None
    target: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "payload": to_jsonable(self.payload),
            "after_hours": self.after_hours,
            "target": self.target,
        }


@dataclass(slots=True)
class Decision:
    """The complete, replayable outcome of evaluating one ticket."""

    policy_name: str
    policy_version: str
    policy_hash: str
    evaluated_at: datetime
    fields: dict[str, Any]
    fired: list[str] = field(default_factory=list)
    trace: list[TraceEntry] = field(default_factory=list)
    notifications: list[Notification] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    required_review: str | None = None
    stopped: bool = False
    scope: dict[str, Any] = field(default_factory=dict)

    def as_dict(self, *, include_scope: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "policy": {
                "name": self.policy_name,
                "version": self.policy_version,
                "hash": self.policy_hash,
            },
            "evaluated_at": self.evaluated_at.isoformat(),
            "fields": to_jsonable(self.fields),
            "fired": list(self.fired),
            "trace": [entry.as_dict() for entry in self.trace],
            "notifications": [item.as_dict() for item in self.notifications],
            "warnings": list(self.warnings),
            "notes": list(self.notes),
            "required_review": self.required_review,
            "stopped": self.stopped,
        }
        if include_scope:
            payload["scope"] = to_jsonable(self.scope)
        return payload


class Engine:
    """Evaluates a compiled policy against ticket-shaped mappings."""

    def __init__(self, policy: CompiledPolicy, *, budget: int = 100_000) -> None:
        self.policy = policy
        self.budget = budget

    # -- public API ------------------------------------------------------
    def evaluate(
        self,
        ticket: Mapping[str, Any],
        *,
        now: datetime,
        operator: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
        include_scope: bool = False,
        duration: float | None = None,
    ) -> Decision:
        """Replay the policy.

        ``duration`` pins the SLA budget instead of re-deriving it from the
        rules.  That is what the periodic refresh uses: recalculating *when* a
        target falls due (because a holiday was added, or the calendar changed)
        must never silently renegotiate *what* the target was.
        """
        if now.tzinfo is None:
            raise RuleEvaluationError("'now' must be timezone-aware")
        calendar = self.policy.calendar
        functions = FunctionLibrary(now, calendar)
        evaluator = Evaluator(functions, budget=self.budget)

        draft = dict(ticket)
        decision = Decision(
            policy_name=self.policy.name,
            policy_version=self.policy.version,
            policy_hash=self.policy.source_hash,
            evaluated_at=now,
            fields={},
        )
        scope = self._scope(draft, now, calendar, operator, extra)

        halted = False
        for compiled in self.policy.rules:
            if not self._matches(compiled, scope, evaluator):
                continue
            decision.fired.append(compiled.rule.name)
            self._apply(compiled, draft, scope, evaluator, decision, now, calendar, operator, extra)
            # Later rules observe earlier effects, so the scope is rebuilt.
            scope = self._scope(draft, now, calendar, operator, extra)
            if compiled.rule.stop or decision.stopped:
                halted = True
            if halted or self.policy.strategy == "first_match":
                decision.stopped = True
                break

        if not decision.fired:
            for compiled in self.policy.fallback:
                if not self._matches(compiled, scope, evaluator):
                    continue
                decision.fired.append(compiled.rule.name)
                self._apply(compiled, draft, scope, evaluator, decision, now, calendar, operator, extra)
                scope = self._scope(draft, now, calendar, operator, extra)

        final_fields = self._resolve_sla(draft, now, calendar, decision, pinned_duration=duration)
        # The decision exposes the *whole* post-policy ticket, not just the SLA
        # slice, so callers can persist tags/labels/team without re-deriving them.
        decision.fields = {**draft, **final_fields}
        if include_scope:
            decision.scope = scope.as_dict()
        return decision

    # -- internals -------------------------------------------------------
    def _scope(
        self,
        draft: Mapping[str, Any],
        now: datetime,
        calendar: BusinessCalendar,
        operator: Mapping[str, Any] | None,
        extra: Mapping[str, Any] | None,
    ) -> Scope:
        ticket = dict(draft)
        created = _parse_ts(ticket.get("created_at")) or now
        policy_view = {
            "name": self.policy.name,
            "version": self.policy.version,
            "hash": self.policy.source_hash,
            "calendar": {
                "day_start": calendar.day_start.isoformat(),
                "day_end": calendar.day_end.isoformat(),
                "tz_offset_minutes": calendar.tz_offset_minutes,
                "holidays": sorted(day.isoformat() for day in calendar.holidays),
                "make_up_workdays": sorted(day.isoformat() for day in calendar.make_up_workdays),
            },
        }
        scope = build_scope(now=now, ticket=ticket, policy=policy_view, operator=operator, extra=extra)
        return scope.derive(
            age_hours=lambda: round((now - created).total_seconds() / 3600.0, 4),
            business_age_hours=lambda: calendar.business_hours_between(created, now),
            is_business_day=lambda: calendar.is_business_day(now.astimezone(calendar.tzinfo).date()),
            in_quiet_hours=lambda: calendar.is_quiet_hours(now),
        )

    def _matches(self, compiled: CompiledRule, scope: Scope, evaluator: Evaluator) -> bool:
        if compiled.condition is None:
            return True
        try:
            return truthy(evaluator.evaluate(compiled.condition, scope))
        except RuleEvaluationError as exc:
            # A rule that cannot be evaluated is skipped, never treated as a match.
            raise RuleEvaluationError(f"rule {compiled.rule.name!r}: {exc}") from exc

    def _apply(
        self,
        compiled: CompiledRule,
        draft: dict[str, Any],
        scope: Scope,
        evaluator: Evaluator,
        decision: Decision,
        now: datetime,
        calendar: BusinessCalendar,
        operator: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        """Run a rule's actions in order.

        The scope is rebuilt after every action so that a later action in the
        same rule sees the earlier one's effect: ``set_priority:P1`` followed by
        ``set_sla:duration`` must measure against the ticket *as amended*, not as
        it arrived.
        """
        for action in compiled.rule.actions:
            if action.code == "stop":
                decision.trace.append(TraceEntry(compiled.rule.name, "stop"))
                decision.stopped = True
                continue
            try:
                self._apply_one(compiled, action, draft, scope, evaluator, decision, now, calendar)
            except RuleEvaluationError as exc:
                decision.trace.append(
                    TraceEntry(compiled.rule.name, action.code, note=str(exc), ok=False)
                )
                raise RuleEvaluationError(f"rule {compiled.rule.name!r} action {action.code}: {exc}") from exc
            scope = self._scope(draft, now, calendar, operator, extra)

    def _apply_one(
        self,
        compiled: CompiledRule,
        action: Action,
        draft: dict[str, Any],
        scope: Scope,
        evaluator: Evaluator,
        decision: Decision,
        now: datetime,
        calendar: BusinessCalendar,
    ) -> None:
        rule_name = compiled.rule.name
        code = action.code

        def record(before: Any, after: Any, note: str = "") -> None:
            decision.trace.append(TraceEntry(rule_name, code, before=before, after=after, note=note))

        if code == "set_priority":
            before = draft.get("priority")
            value = str(action.args[0]).upper()
            draft["priority"] = value
            record(before, value)
            return

        if code == "assign_team":
            before = draft.get("team")
            value = str(action.args[0])
            draft["team"] = value
            record(before, value)
            return

        if code == "set_sla:duration":
            hours = self._eval_hours(action, scope, evaluator, "set_sla:duration")
            if hours < 0:
                raise RuleEvaluationError("SLA duration cannot be negative")
            before = draft.get("sla_duration_hours")
            draft["sla_duration_hours"] = hours
            record(before, hours)
            return

        if code == "set_sla:snooze":
            hours = self._eval_hours(action, scope, evaluator, "set_sla:snooze")
            if hours < 0:
                raise RuleEvaluationError("SLA snooze cannot be negative")
            before = draft.get("sla_snooze_hours", 0.0)
            draft["sla_snooze_hours"] = hours
            record(before, hours)
            return

        if code == "set_sla:calendar":
            before = draft.get("sla_calendar", "business")
            value = str(action.args[0])
            draft["sla_calendar"] = value
            record(before, value)
            return

        if code == "sla_recompute":
            before = draft.get("sla_due_at")
            resolved = self._resolve_sla(draft, now, calendar, decision)
            record(before, resolved.get("sla_due_at"), "due timestamp recomputed")
            return

        if code == "escalate:after":
            hours = self._eval_hours(action, scope, evaluator, "escalate:after")
            before = draft.get("escalation_after_hours")
            draft["escalation_after_hours"] = hours
            record(before, hours)
            return

        if code == "escalate:to":
            before = draft.get("escalation_target")
            value = str(action.args[0])
            draft["escalation_target"] = value
            record(before, value)
            return

        if code == "escalate:notify":
            channel = str(action.args[0])
            payload = [to_jsonable(arg) for arg in action.args[1:]] or [str(draft.get("id", "ticket"))]
            notification = Notification(
                channel=channel,
                payload=payload[0] if len(payload) == 1 else payload,
                after_hours=float(draft["escalation_after_hours"])
                if isinstance(draft.get("escalation_after_hours"), (int, float))
                else None,
                target=str(draft["escalation_target"]) if draft.get("escalation_target") else None,
            )
            decision.notifications.append(notification)
            record(None, f"{channel}:{notification.payload}")
            return

        if code == "tag":
            tags = list(draft.get("tags") or [])
            added = [str(arg) for arg in action.args]
            before = list(tags)
            for value in added:
                if value not in tags:
                    tags.append(value)
            draft["tags"] = tags
            record(before, tags)
            return

        if code == "label":
            labels = dict(draft.get("labels") or {})
            key, value = str(action.args[0]), to_jsonable(action.args[1])
            before = labels.get(key)
            labels[key] = value
            draft["labels"] = labels
            record(before, value, key)
            return

        if code == "require_review":
            reason = str(action.args[0]) if action.args else "policy requires review"
            decision.required_review = reason
            record(None, reason)
            return

        if code == "note":
            text = str(action.args[0])
            decision.notes.append(text)
            record(None, text)
            return

        raise RuleEvaluationError(f"unhandled action {code!r}")  # pragma: no cover

    def _eval_hours(
        self, action: Action, scope: Scope, evaluator: Evaluator, where: str
    ) -> float:
        raw = action.args[0]
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
        if isinstance(raw, str):
            text = raw.strip()
            try:  # a bare number is the common case; skip the parser entirely
                return float(text)
            except ValueError:
                pass
            node = parse(text)
            value = evaluator.evaluate(node, scope)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RuleEvaluationError(f"{where}: expression {text!r} did not yield a number")
            return float(value)
        raise RuleEvaluationError(f"{where}: unsupported argument {raw!r}")

    def _resolve_sla(
        self,
        draft: dict[str, Any],
        now: datetime,
        calendar: BusinessCalendar,
        decision: Decision,
        pinned_duration: float | None = None,
    ) -> dict[str, Any]:
        """Turn ``{duration, calendar, snooze}`` into concrete timestamps."""
        created = _parse_ts(draft.get("created_at")) or now
        closed_at = _parse_ts(draft.get("closed_at"))
        duration: Any = pinned_duration if pinned_duration is not None else draft.get("sla_duration_hours")
        if duration is None or not isinstance(duration, (int, float)):
            duration = self.policy.defaults.get("sla_duration_hours")
        if isinstance(duration, (int, float)):
            duration = float(duration)
            mode = str(draft.get("sla_calendar") or self.policy.defaults.get("sla_calendar") or "business")
            snooze = float(draft.get("sla_snooze_hours") or 0.0)
            effective_hours = max(0.0, duration - snooze)
            if mode == "wall":
                due = created + timedelta(hours=effective_hours)
                reference = min(now, closed_at) if closed_at else now
                consumed = (reference - created).total_seconds() / 3600.0
            else:
                due = calendar.add_business_hours(created, effective_hours)
                reference = min(now, closed_at) if closed_at else now
                consumed = calendar.business_hours_between(created, reference)
            draft["sla_duration_hours"] = duration
            draft["sla_snooze_hours"] = snooze
            draft["sla_calendar"] = mode
            draft["sla_effective_hours"] = round(effective_hours, 4)
            draft["sla_due_at"] = due
            draft["sla_consumed_hours"] = round(consumed, 4)
            draft["sla_remaining_hours"] = round(effective_hours - consumed, 4)
        else:
            decision.warnings.append("no SLA duration resolved; ticket is untracked")

        escalation_after = draft.get("escalation_after_hours")
        if isinstance(escalation_after, (int, float)):
            draft["escalation_at"] = (
                calendar.add_business_hours(created, float(escalation_after))
                if str(draft.get("sla_calendar") or "business") == "business"
                else created + timedelta(hours=float(escalation_after))
            )
        return {
            key: value
            for key, value in draft.items()
            if key.startswith("sla") or key.startswith("escalation")
        }


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else None
    return None


def describe_actions() -> list[dict[str, str]]:
    return [{"code": code, "docs": docs} for code, docs in sorted(ACTION_CATALOG.items())]


def describe_functions() -> list[dict[str, str]]:
    return [{"signature": sig, "docs": doc} for sig, doc in SIGNATURES.items()]


def policy_summary(policy: CompiledPolicy) -> dict[str, Any]:
    return {
        "name": policy.name,
        "version": policy.version,
        "strategy": policy.strategy,
        "hash": policy.source_hash,
        "calendar": policy.calendar.describe(),
        "rules": [
            {
                "name": item.rule.name,
                "when": item.rule.when,
                "description": item.rule.description,
                "priority": item.rule.priority,
                "stop": item.rule.stop,
                "tags": list(item.rule.tags),
                "actions": [action.as_dict() for action in item.rule.actions],
            }
            for item in policy.rules
        ],
        "fallback": [
            {"name": item.rule.name, "when": item.rule.when, "actions": [a.as_dict() for a in item.rule.actions]}
            for item in policy.fallback
        ],
        "defaults": to_jsonable(dict(policy.defaults)),
        "functions": sorted(KNOWN_FUNCTIONS),
    }
