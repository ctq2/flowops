"""Domain layer: entities, value objects and the rule engine."""

from .errors import (
    ConflictError,
    DomainError,
    IllegalTransitionError,
    NotFoundError,
    PreconditionFailedError,
    RuleError,
    RuleEvaluationError,
    RuleSyntaxError,
    ValidationError,
)
from .ticket import Ticket
from .values import Priority, RiskBand, SlaState, Status, allowed_transitions, band_for, can_transition

__all__ = [
    "ConflictError",
    "DomainError",
    "IllegalTransitionError",
    "NotFoundError",
    "PreconditionFailedError",
    "Priority",
    "RiskBand",
    "RuleError",
    "RuleEvaluationError",
    "RuleSyntaxError",
    "SlaState",
    "Status",
    "Ticket",
    "ValidationError",
    "allowed_transitions",
    "band_for",
    "can_transition",
]
