"""Domain error hierarchy.

Every error raised by the domain layer derives from :class:`DomainError`, so the
HTTP adapter can translate the whole family in one place instead of guessing
which exception means which status code.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for every expected (non-bug) failure in the domain."""

    kind = "about:blank"
    title = "Domain error"
    status = 400

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


class ValidationError(DomainError):
    kind = "https://flowops.dev/problems/validation"
    title = "Validation failed"
    status = 422


class NotFoundError(DomainError):
    kind = "https://flowops.dev/problems/not-found"
    title = "Resource not found"
    status = 404


class ConflictError(DomainError):
    kind = "https://flowops.dev/problems/conflict"
    title = "State conflict"
    status = 409


class PreconditionFailedError(DomainError):
    """Raised when a write loses an optimistic-concurrency race (ETag mismatch)."""

    kind = "https://flowops.dev/problems/precondition-failed"
    title = "Precondition failed"
    status = 412


class IllegalTransitionError(ConflictError):
    kind = "https://flowops.dev/problems/illegal-transition"
    title = "Illegal status transition"


class RuleError(ValidationError):
    """Base class for every failure produced by the rule engine."""

    kind = "https://flowops.dev/problems/rule"
    title = "Rule error"


class RuleSyntaxError(RuleError):
    """A rule expression could not be parsed."""

    kind = "https://flowops.dev/problems/rule-syntax"
    title = "Rule syntax error"

    def __init__(self, detail: str, *, source: str = "", offset: int = 0) -> None:
        # Render a caret line so operators can see exactly where the parser gave up.
        pointer = ""
        if source:
            line_start = source.rfind("\n", 0, offset) + 1
            line_end = source.find("\n", offset)
            if line_end == -1:
                line_end = len(source)
            excerpt = source[line_start:line_end]
            caret = " " * max(0, offset - line_start) + "^"
            pointer = f"\n{excerpt}\n{caret}"
        super().__init__(f"{detail}{pointer}", source=source, offset=offset)


class RuleEvaluationError(RuleError):
    kind = "https://flowops.dev/problems/rule-evaluation"
    title = "Rule evaluation error"


class RuleBudgetExceeded(RuleEvaluationError):
    kind = "https://flowops.dev/problems/rule-budget"
    title = "Rule evaluation budget exceeded"
