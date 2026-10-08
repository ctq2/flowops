"""Tree-walking evaluator.

Comparison semantics are deliberately strict.  A policy language that answers
``"urgent" < 5`` with a boolean, or that compares a missing field as ``null``
instead of failing loudly, is a policy language that will one day silently route
a P0 outage to the wrong team.  Missing fields raise; cross-type ordering raises;
only ``==``/``!=`` compare across types (and then always by identity, never by
coercion).
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from ..errors import RuleBudgetExceeded, RuleEvaluationError
from .ast import Binary, Call, Field, ListLiteral, Literal, Node, Unary
from .functions import FunctionLibrary
from .scope import Scope

#: Hard ceiling on AST nodes visited for a single expression evaluation.
DEFAULT_BUDGET = 100_000


def truthy(value: Any) -> bool:
    """Rule-language truthiness: only real booleans are true.

    ``0``, ``""`` and empty lists are *false*; anything else non-boolean is an
    error rather than a silent conversion.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (str, list, tuple, dict, set)):
        return len(value) > 0
    raise RuleEvaluationError(f"cannot interpret {type(value).__name__} as a condition")


def to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    return value


class Evaluator:
    def __init__(self, functions: FunctionLibrary, budget: int = DEFAULT_BUDGET) -> None:
        self.functions = functions
        self.budget = budget
        self.steps = 0

    # -- entry point -----------------------------------------------------
    def evaluate(self, node: Node, scope: Scope) -> Any:
        self.steps = 0
        return self._eval(node, scope)

    # -- dispatch --------------------------------------------------------
    def _tick(self) -> None:
        self.steps += 1
        if self.steps > self.budget:
            raise RuleBudgetExceeded(
                f"expression exceeded the evaluation budget of {self.budget} steps"
            )

    def _eval(self, node: Node, scope: Scope) -> Any:
        self._tick()
        if isinstance(node, Literal):
            return node.value
        if isinstance(node, Field):
            return scope.resolve(node.dotted)
        if isinstance(node, ListLiteral):
            return [self._eval(item, scope) for item in node.items]
        if isinstance(node, Unary):
            return self._unary(node, scope)
        if isinstance(node, Binary):
            return self._binary(node, scope)
        if isinstance(node, Call):
            args = [self._eval(arg, scope) for arg in node.args]
            return self.functions.call(node.name, args)
        raise RuleEvaluationError(f"cannot evaluate node {node!r}")  # pragma: no cover

    # -- operators -------------------------------------------------------
    def _unary(self, node: Unary, scope: Scope) -> Any:
        operand = self._eval(node.operand, scope)
        if node.op == "not":
            return not truthy(operand)
        if node.op == "-":
            return -self._number(operand, "-")
        raise RuleEvaluationError(f"unknown unary operator {node.op!r}")  # pragma: no cover

    def _binary(self, node: Binary, scope: Scope) -> Any:
        op = node.op
        # Short-circuit before touching the right operand.
        if op in ("and", "or"):
            left = truthy(self._eval(node.left, scope))
            if op == "and":
                return self._eval_bool(node.right, scope) if left else False
            return True if left else self._eval_bool(node.right, scope)

        left = self._eval(node.left, scope)
        right = self._eval(node.right, scope)

        if op == "==":
            return self._equals(left, right)
        if op == "!=":
            return not self._equals(left, right)
        if op == "<":
            return self._order(left, right) < 0
        if op == "<=":
            return self._order(left, right) <= 0
        if op == ">":
            return self._order(left, right) > 0
        if op == ">=":
            return self._order(left, right) >= 0
        if op == "in":
            return self._contains(left, right)
        if op == "+":
            # `null` is treated as an empty string when the other side is a
            # string: a missing optional field should not break message
            # formatting.  Two non-strings must still agree on their type.
            if isinstance(left, str) or isinstance(right, str):
                if left is None or right is None:
                    return ("" if left is None else left) + ("" if right is None else right)  # type: ignore[operator]
                if not isinstance(left, str) or not isinstance(right, str):
                    raise RuleEvaluationError("'+' cannot mix strings and numbers")
                return left + right
            if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
                return [*left, *right]
            if left is None or right is None:
                raise RuleEvaluationError("'+' cannot combine null with a number")
            return self._number(left, "+") + self._number(right, "+")
        if op == "-":
            return self._number(left, "-") - self._number(right, "-")
        if op == "*":
            if isinstance(left, str) and isinstance(right, int):
                return left * right
            if isinstance(right, str) and isinstance(left, int):
                return right * left
            return self._number(left, "*") * self._number(right, "*")
        if op == "/":
            divisor = self._number(right, "/")
            if divisor == 0:
                raise RuleEvaluationError("division by zero")
            return self._number(left, "/") / divisor
        if op == "%":
            divisor = self._number(right, "%")
            if divisor == 0:
                raise RuleEvaluationError("modulo by zero")
            return self._number(left, "%") % divisor
        raise RuleEvaluationError(f"unknown operator {op!r}")  # pragma: no cover

    def _eval_bool(self, node: Node, scope: Scope) -> bool:
        return truthy(self._eval(node, scope))

    @staticmethod
    def _number(value: Any, op: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuleEvaluationError(f"operator '{op}' needs numbers, got {type(value).__name__}")
        return float(value)

    @staticmethod
    def _equals(left: Any, right: Any) -> bool:
        if isinstance(left, bool) != isinstance(right, bool):
            return False
        if isinstance(left, datetime) and isinstance(right, datetime):
            return left == right
        if isinstance(left, (int, float)) and isinstance(right, (int, float)) and not isinstance(left, bool):
            return float(left) == float(right)
        if type(left) is not type(right):
            return False
        return bool(left == right)

    @staticmethod
    def _order(left: Any, right: Any) -> int:
        if isinstance(left, datetime) and isinstance(right, datetime):
            return (left > right) - (left < right)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)) and not (
            isinstance(left, bool) or isinstance(right, bool)
        ):
            return (left > right) - (left < right)
        if isinstance(left, str) and isinstance(right, str):
            return (left > right) - (left < right)
        raise RuleEvaluationError(
            f"cannot order {type(left).__name__} against {type(right).__name__}"
        )

    @staticmethod
    def _contains(left: Any, right: Any) -> bool:
        if isinstance(right, (list, tuple, set)):
            return any(Evaluator._equals(left, item) for item in right)
        if isinstance(right, str):
            if not isinstance(left, str):
                raise RuleEvaluationError("'in' on a string needs a string on the left")
            return left in right
        if isinstance(right, dict):
            return left in right
        raise RuleEvaluationError(f"'in' needs a list, string or object, got {type(right).__name__}")
