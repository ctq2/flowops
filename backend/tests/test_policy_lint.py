"""Tests for the policy linter — it must catch real rot without crying wolf."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.domain.rules.engine import Policy, compile_policy
from app.policy_lint import SEVERITY_ORDER, lint_policy

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "policies" / "default.json"


def codes(report) -> list[str]:
    return [finding.code for finding in report.findings]


class ShippedPolicyTests(unittest.TestCase):
    def test_the_shipped_policy_is_clean(self) -> None:
        document = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        policy = Policy.from_dict(document)
        report = lint_policy(policy.compiled, document=document)
        self.assertTrue(report.ok, [finding.message for finding in report.findings])
        self.assertNotIn("unreachable", codes(report))
        self.assertNotIn("duplicate-condition", codes(report))

    def test_stats_describe_the_policy(self) -> None:
        policy = Policy.from_file(POLICY_PATH)
        report = lint_policy(policy.compiled, document=policy.document)
        self.assertGreaterEqual(report.stats["rules"], 8)
        self.assertEqual(report.stats["strategy"], "first_match")
        self.assertIn("set_priority", report.stats["actions_used"])


class UnreachableRuleTests(unittest.TestCase):
    def test_a_rule_after_an_unconditional_one_is_flagged_as_an_error(self) -> None:
        policy = compile_policy(
            {
                "name": "shadowing",
                "rules": [
                    {"name": "catch-all", "when": None, "then": ["tag:everything"]},
                    {"name": "never-runs", "when": "ticket.priority == 'P0'", "then": ["tag:p0"]},
                ],
            }
        )
        report = lint_policy(policy)
        self.assertFalse(report.ok)
        self.assertIn("unreachable", codes(report))
        finding = next(item for item in report.findings if item.code == "unreachable")
        self.assertEqual(finding.rule, "never-runs")
        self.assertIn("catch-all", finding.message)

    def test_an_unconditional_rule_that_is_first_is_fine(self) -> None:
        policy = compile_policy(
            {
                "name": "normal",
                "rules": [
                    {"name": "specific", "when": "ticket.priority == 'P0'", "then": ["tag:p0"]},
                    {"name": "catch-all", "when": None, "then": ["tag:everything"]},
                ],
            }
        )
        report = lint_policy(policy)
        self.assertTrue(report.ok)
        self.assertNotIn("unreachable", codes(report))

    def test_accumulate_strategy_does_not_accuse_rules_of_being_unreachable(self) -> None:
        # Under `accumulate` every matching rule runs, so a specific rule after an
        # unconditional one still fires — reporting it as dead would be wrong.
        policy = compile_policy(
            {
                "name": "accumulating",
                "strategy": "accumulate",
                "rules": [
                    {"name": "first", "when": None, "then": ["tag:a"]},
                    {"name": "second", "when": "ticket.priority == 'P0'", "then": ["tag:b"]},
                ],
            }
        )
        report = lint_policy(policy)
        self.assertTrue(report.ok)
        self.assertNotIn("unreachable", codes(report))
        self.assertNotIn("unconditional-rule-not-first", codes(report))

    def test_a_narrow_rule_before_a_catch_all_is_never_flagged(self) -> None:
        policy = compile_policy(
            {
                "name": "ordered",
                "rules": [
                    {"name": "narrow", "when": "ticket.priority == 'P0'", "then": ["tag:p0"]},
                    {"name": "rest", "when": "ticket.priority == 'P1'", "then": ["tag:p1"]},
                ],
                "fallback": [{"name": "f", "when": None, "then": ["tag:other"]}],
            }
        )
        report = lint_policy(policy)
        self.assertTrue(report.ok)
        self.assertEqual([finding.code for finding in report.findings], [])


class DuplicateConditionTests(unittest.TestCase):
    def test_identical_conditions_are_reported(self) -> None:
        policy = compile_policy(
            {
                "name": "dup",
                "rules": [
                    {"name": "one", "when": "ticket.team == 'payments'", "then": ["tag:a"]},
                    {"name": "two", "when": "ticket.team == 'payments'", "then": ["tag:b"]},
                ],
            }
        )
        report = lint_policy(policy)
        self.assertIn("duplicate-condition", codes(report))
        finding = next(item for item in report.findings if item.code == "duplicate-condition")
        self.assertIn("one", finding.message)

    def test_equivalent_but_differently_written_conditions_are_not_reported(self) -> None:
        # Whitespace and parenthesisation differ, but the linter compares the
        # normalised AST rendering, so this is genuinely the same condition and
        # *is* reported — while a different constant is not.
        policy = compile_policy(
            {
                "name": "not-dup",
                "rules": [
                    {"name": "one", "when": "ticket.team == 'payments'", "then": ["tag:a"]},
                    {"name": "two", "when": "ticket.team == 'platform'", "then": ["tag:b"]},
                ],
            }
        )
        self.assertNotIn("duplicate-condition", codes(lint_policy(policy)))


class MissingFallbackTests(unittest.TestCase):
    def test_no_fallback_produces_an_info_finding(self) -> None:
        policy = compile_policy(
            {"name": "bare", "rules": [{"name": "only", "when": "ticket.priority == 'P0'", "then": ["tag:p0"]}]}
        )
        report = lint_policy(policy)
        self.assertIn("no-fallback", codes(report))
        self.assertIn("last-rule-is-specific", codes(report))
        self.assertTrue(report.ok, "informational findings must not fail the gate")


class UnknownActionTests(unittest.TestCase):
    def test_an_action_that_only_exists_as_a_namespace_is_an_error(self) -> None:
        # `set_sla` alone is not an action; the real ones are `set_sla:duration`
        # and friends. Reading the document as written catches the intent even
        # though the compiled policy would already have rejected it.
        document = {
            "name": "typo",
            "rules": [{"name": "r", "when": None, "then": ["tag:x"]}],
        }
        policy = compile_policy(document)
        broken = dict(document)
        broken["rules"] = [{"name": "r", "when": None, "then": ["set_sla:4"]}]
        report = lint_policy(policy, document=broken)
        self.assertIn("unknown-action", codes(report))
        finding = next(item for item in report.findings if item.code == "unknown-action")
        self.assertIn("set_sla", finding.message)

    def test_valid_namespaced_shorthand_is_not_reported(self) -> None:
        document = {
            "name": "fine",
            "rules": [
                {
                    "name": "r",
                    "when": None,
                    "then": ["escalate:to:oncall-sre", "escalate:notify:im", "set_sla:calendar:business"],
                }
            ],
            "fallback": [{"name": "f", "when": None, "then": ["tag:x"]}],
        }
        report = lint_policy(compile_policy(document), document=document)
        self.assertNotIn("unknown-action", codes(report))
        self.assertEqual([item.message for item in report.findings], [])


class ReportShapeTests(unittest.TestCase):
    def test_report_serialises_for_ci_consumption(self) -> None:
        policy = Policy.from_file(POLICY_PATH)
        report = lint_policy(policy.compiled, document=policy.document)
        payload = report.as_dict()
        self.assertIn("ok", payload)
        self.assertIn("counts", payload)
        self.assertIn("stats", payload)
        self.assertIsInstance(payload["findings"], list)

    def test_findings_are_sorted_by_severity(self) -> None:
        policy = compile_policy(
            {
                "name": "mixed",
                "rules": [
                    {"name": "catch-all", "when": None, "then": ["tag:a"]},
                    {"name": "never", "when": "ticket.priority == 'P0'", "then": ["tag:b"]},
                ],
            }
        )
        report = lint_policy(policy)
        severities = [SEVERITY_ORDER[finding.severity] for finding in report.sorted()]
        self.assertEqual(severities, sorted(severities))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
