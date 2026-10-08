"""Evaluation scope: the read-only data a rule can see.

A scope is a thin, JSON-shaped wrapper around the values the engine exposes to
rules.  Derived fields (age, SLA remaining, business hours consumed …) are
computed once per evaluation and cached, so a policy with 30 rules does not
recompute calendar arithmetic 30 times.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator, Mapping

from ..errors import RuleEvaluationError


class Scope:
    """Nested field lookup with dot notation and lazy derived fields."""

    __slots__ = ("_data", "_derived", "_cache")

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = dict(data or {})
        self._derived: dict[str, Any] = {}
        self._cache: dict[str, Any] = {}

    # -- composition -----------------------------------------------------
    def with_values(self, **values: Any) -> "Scope":
        """Return a child scope that overlays ``values`` on top of this one."""
        child = Scope(self._data)
        child._data.update(values)
        child._derived = dict(self._derived)
        return child

    def derive(self, **fields: Any) -> "Scope":
        """Register lazily-computed fields (name -> zero-argument callable)."""
        child = Scope(self._data)
        child._derived = {**self._derived, **fields}
        return child

    # -- access ----------------------------------------------------------
    def resolve(self, path: str) -> Any:
        """Look up a dotted path.

        Two deliberate asymmetries make policies robust without becoming sloppy:

        * an unknown **top-level** name is an error — that is a typo the author
          must fix;
        * a missing **nested** key resolves to ``None`` — ``ticket.labels.tier``
          is legitimately absent on most tickets, and forcing every author to
          guard it would make the language unusable.

        ``None`` never compares equal to anything, so a missing field can never
        silently satisfy a condition.
        """
        parts = path.split(".") if path else []
        if not parts:
            raise RuleEvaluationError("empty field reference")
        value = self._lookup(parts[0])
        for part in parts[1:]:
            if value is None:
                return None
            if isinstance(value, Mapping):
                if part not in value:
                    return None
                value = value[part]
            else:
                raise RuleEvaluationError(
                    f"field '{'.'.join(parts[:2])}' is not a record "
                    f"(got {type(value).__name__})"
                )
        return value

    def _lookup(self, name: str) -> Any:
        if name in self._cache:
            return self._cache[name]
        if name in self._data:
            return self._data[name]
        if name in self._derived:
            value = self._derived[name]()
            self._cache[name] = value
            return value
        raise RuleEvaluationError(f"unknown field '{name}'")

    def has(self, path: str) -> bool:
        try:
            self.resolve(path)
        except RuleEvaluationError:
            return False
        return True

    def get(self, path: str, default: Any = None) -> Any:
        try:
            return self.resolve(path)
        except RuleEvaluationError:
            return default

    def as_dict(self) -> dict[str, Any]:
        """Materialise everything the scope can see — used by the dry-run API."""
        out = dict(self._data)
        for name in self._derived:
            try:
                out[name] = self._lookup(name)
            except RuleEvaluationError:
                continue
        return out

    def __iter__(self) -> Iterator[str]:
        return iter(sorted({*self._data, *self._derived}))


def build_scope(
    *,
    now: datetime,
    ticket: Mapping[str, Any],
    policy: Mapping[str, Any] | None = None,
    operator: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Scope:
    """Assemble the documented scope surface exposed to policy authors.

    See ``docs/rules-dsl.md`` for the public contract; anything not listed there
    is intentionally not reachable from a rule.
    """
    scope = Scope({"now": now, "ticket": dict(ticket), "policy": dict(policy or {})})
    if operator:
        scope = scope.with_values(operator=dict(operator))
    if extra:
        scope = scope.with_values(**dict(extra))
    return scope
