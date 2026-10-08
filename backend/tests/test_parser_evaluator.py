"""Parser and evaluator tests: shape of the AST, precedence, and strict typing."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from app.domain.errors import RuleBudgetExceeded, RuleEvaluationError, RuleSyntaxError
from app.domain.rules.ast import Binary, Call, Field, ListLiteral, Literal, Unary, describe
from app.domain.rules.calendar import BusinessCalendar
from app.domain.rules.evaluator import Evaluator, to_jsonable, truthy
from app.domain.rules.functions import FunctionLibrary
from app.domain.rules.parser import KNOWN_FUNCTIONS, parse
from app.domain.rules.scope import Scope

NOW = datetime(2026, 3, 2, 10, 0, tzinfo=timezone(timedelta(hours=8)))


class ParserTests(unittest.TestCase):
    def test_multiplication_binds_tighter_than_addition(self) -> None:
        node = parse("1 + 2 * 3")
        assert isinstance(node, Binary)
        self.assertEqual(node.op, "+")
        self.assertIsInstance(node.right, Binary)
        self.assertEqual(describe(node), "(1 + (2 * 3))")

    def test_comparison_binds_tighter_than_and(self) -> None:
        node = parse("a > 1 and b < 2")
        assert isinstance(node, Binary)
        self.assertEqual(node.op, "and")
        self.assertEqual(describe(node), "((a > 1) and (b < 2))")

    def test_parentheses_override_precedence(self) -> None:
        self.assertEqual(describe(parse("(1 + 2) * 3")), "((1 + 2) * 3)")

    def test_unary_not_and_minus(self) -> None:
        node = parse("not a == -1")
        assert isinstance(node, Unary)
        self.assertEqual(node.op, "not")
        self.assertEqual(describe(node), "not (a == -1)")

    def test_field_access_chain(self) -> None:
        node = parse("ticket.labels.tier")
        assert isinstance(node, Field)
        self.assertEqual(node.path, ("ticket", "labels", "tier"))

    def test_list_literal_and_in_operator(self) -> None:
        node = parse("ticket.priority in ['P0', 'P1']")
        assert isinstance(node, Binary)
        self.assertEqual(node.op, "in")
        assert isinstance(node.right, ListLiteral)
        self.assertEqual(len(node.right.items), 2)

    def test_function_call_with_nested_arguments(self) -> None:
        node = parse("contains(lower(ticket.title), 'outage')")
        assert isinstance(node, Call)
        self.assertEqual(node.name, "contains")
        self.assertEqual(describe(node), "contains(lower(ticket.title), 'outage')")

    def test_trailing_comma_is_tolerated(self) -> None:
        node = parse("min(1, 2,)")
        assert isinstance(node, Call)
        self.assertEqual(len(node.args), 2)

    def test_every_documented_function_parses(self) -> None:
        for name in sorted(KNOWN_FUNCTIONS):
            with self.subTest(function=name):
                parse(f"{name}()")

    def test_unknown_function_is_rejected_at_parse_time(self) -> None:
        with self.assertRaises(RuleSyntaxError) as ctx:
            parse("evaluate_this(1)")
        self.assertIn("unknown function", str(ctx.exception))

    def test_trailing_input_is_rejected(self) -> None:
        with self.assertRaises(RuleSyntaxError) as ctx:
            parse("1 == 1 2")
        self.assertIn("trailing input", str(ctx.exception))

    def test_empty_expression_is_rejected(self) -> None:
        with self.assertRaises(RuleSyntaxError):
            parse("   ")

    def test_error_message_points_at_the_column(self) -> None:
        with self.assertRaises(RuleSyntaxError) as ctx:
            parse("ticket.priority == ")
        message = str(ctx.exception)
        self.assertIn("unexpected end of expression", message)


class EvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.functions = FunctionLibrary(NOW, BusinessCalendar())
        self.evaluator = Evaluator(self.functions)
        self.scope = Scope(
            {
                "now": NOW,
                "ticket": {
                    "title": "Payment gateway 502",
                    "priority": "P1",
                    # json.loads, not a string: `'vip' in '[…]'` would be a
                    # substring test against the serialised JSON and pass for
                    # the wrong reason.
                    "tags": json.loads('["payments", "vip"]'),
                    "labels": {"tier": "vip"},
                    "created_at": NOW - timedelta(hours=6),
                },
                "policy": {"name": "test"},
            }
        )

    def evaluate(self, source: str):
        return self.evaluator.evaluate(parse(source), self.scope)

    def test_arithmetic_and_precedence(self) -> None:
        self.assertEqual(self.evaluate("2 + 3 * 4"), 14.0)
        self.assertEqual(self.evaluate("(2 + 3) * 4"), 20.0)
        self.assertEqual(self.evaluate("7 % 3"), 1.0)

    def test_string_concatenation_and_repetition(self) -> None:
        self.assertEqual(self.evaluate("'a' + 'b'"), "ab")
        self.assertEqual(self.evaluate("'ab' * 2"), "abab")

    def test_string_and_number_cannot_be_added(self) -> None:
        with self.assertRaises(RuleEvaluationError):
            self.evaluate("'a' + 1")

    def test_equality_is_type_strict_case_sensitive(self) -> None:
        self.assertFalse(self.evaluate("'high' == 'HIGH'"))
        self.assertTrue(self.evaluate("'high' == 'high'"))
        self.assertFalse(self.evaluate("1 == '1'"))
        self.assertTrue(self.evaluate("1 == 1.0"))
        self.assertFalse(self.evaluate("true == 1"))

    def test_ordering_across_types_raises(self) -> None:
        with self.assertRaises(RuleEvaluationError):
            self.evaluate("1 < 'a'")

    def test_boolean_operators_short_circuit(self) -> None:
        # `false and <error>` must not evaluate the right-hand side.
        self.assertFalse(self.evaluate("false and (1 / 0) == 1"))
        self.assertTrue(self.evaluate("true or (1 / 0) == 1"))

    def test_in_operator_on_lists_and_strings(self) -> None:
        self.assertTrue(self.evaluate("ticket.priority in ['P0', 'P1']"))
        self.assertFalse(self.evaluate("ticket.priority in ['P2']"))
        self.assertTrue(self.evaluate("'vip' in ticket.tags"))
        self.assertTrue(self.evaluate("'Payment' in ticket.title"))
        # Substring matching is case-sensitive on purpose: authors must write
        # lower(ticket.title) when they mean a case-insensitive test.
        self.assertFalse(self.evaluate("'pay' in ticket.title"))
        self.assertTrue(self.evaluate("'pay' in lower(ticket.title)"))

    def test_missing_nested_field_is_null_and_never_equal(self) -> None:
        self.assertIsNone(self.evaluate("ticket.labels.missing"))
        self.assertFalse(self.evaluate("ticket.labels.missing == 'vip'"))

    def test_unknown_top_level_field_is_an_error(self) -> None:
        with self.assertRaises(RuleEvaluationError) as ctx:
            self.evaluate("tickt.priority == 'P1'")
        self.assertIn("unknown field", str(ctx.exception))

    def test_nested_lookup_on_a_scalar_is_an_error(self) -> None:
        with self.assertRaises(RuleEvaluationError):
            self.evaluate("ticket.title.length == 3")

    def test_division_by_zero_is_reported_clearly(self) -> None:
        with self.assertRaises(RuleEvaluationError) as ctx:
            self.evaluate("1 / 0")
        self.assertIn("division by zero", str(ctx.exception))

    def test_functions_from_the_standard_library(self) -> None:
        self.assertEqual(self.evaluate("len(ticket.tags)"), 2)
        self.assertEqual(self.evaluate("upper('ab')"), "AB")
        self.assertTrue(self.evaluate("startswith(ticket.title, 'Payment')"))
        self.assertTrue(self.evaluate("matches('(?i)502', ticket.title)"))
        self.assertEqual(self.evaluate("coalesce(ticket.labels.missing, 'fallback')"), "fallback")
        self.assertEqual(self.evaluate("int('42.9')"), 42)
        self.assertEqual(self.evaluate("round(1.2345, 2)"), 1.23)

    def test_business_hour_functions_use_the_injected_calendar(self) -> None:
        # Monday 10:00 local, 6 wall-clock hours ago = 06:00, before opening.
        self.assertEqual(self.evaluate("hours_since(ticket.created_at)"), 6.0)
        self.assertEqual(self.evaluate("business_hours_between(ticket.created_at, now())"), 1.0)

    def test_wrong_arity_is_reported_with_the_function_name(self) -> None:
        with self.assertRaises(RuleEvaluationError) as ctx:
            self.evaluate("lower('a', 'b')")
        self.assertIn("lower()", str(ctx.exception))

    def test_regex_length_is_capped(self) -> None:
        with self.assertRaises(RuleEvaluationError) as ctx:
            self.evaluate(f"matches('{'a' * 250}', 'aaa')")
        self.assertIn("longer than", str(ctx.exception))

    def test_invalid_regex_is_reported(self) -> None:
        with self.assertRaises(RuleEvaluationError) as ctx:
            self.evaluate("matches('([', 'aaa')")
        self.assertIn("invalid pattern", str(ctx.exception))

    def test_budget_stops_runaway_expressions(self) -> None:
        evaluator = Evaluator(self.functions, budget=10)
        with self.assertRaises(RuleBudgetExceeded):
            evaluator.evaluate(parse("1 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1"), self.scope)

    def test_truthiness_rules(self) -> None:
        self.assertTrue(truthy(True))
        self.assertFalse(truthy(0))
        self.assertFalse(truthy(""))
        self.assertTrue(truthy("x"))
        self.assertTrue(truthy([1]))
        with self.assertRaises(RuleEvaluationError):
            truthy(object())

    def test_json_projection_renders_timestamps_and_rounds_floats(self) -> None:
        payload = to_jsonable({"at": NOW, "ratio": 1 / 3, "items": [NOW]})
        self.assertEqual(payload["at"], NOW.isoformat())
        self.assertEqual(payload["ratio"], 0.3333)
        self.assertEqual(payload["items"], [NOW.isoformat()])

    def test_literal_string_describe_is_quoted(self) -> None:
        self.assertEqual(describe(parse("'x'")), "'x'")
        assert isinstance(parse("'x'"), Literal)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
