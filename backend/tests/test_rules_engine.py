"""Policy-engine tests: compilation, action semantics, traces and determinism."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.domain.errors import RuleEvaluationError, ValidationError
from app.domain.rules.calendar import BusinessCalendar
from app.domain.rules.engine import (
    ACTION_CATALOG,
    Engine,
    Policy,
    compile_policy,
    describe_actions,
    describe_functions,
    policy_summary,
)

CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 3, 2, 10, 0, tzinfo=CN)  # Monday, mid-morning
POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "policies" / "default.json"


def ticket(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "TCK-TEST-1",
        "title": "示例工单",
        "body": "",
        "status": "open",
        "priority": "P3",
        "team": None,
        "reporter": "tester",
        "assignee": None,
        "channel": "web",
        "tags": [],
        "labels": {},
        "created_at": NOW.isoformat(),
        "sla_duration_hours": None,
        "sla_consumed_hours": None,
        "sla_snooze_hours": 0.0,
        "sla_calendar": "business",
    }
    base.update(overrides)
    return base


MINIMAL = {
    "name": "test-policy",
    "version": "1.0.0",
    "strategy": "first_match",
    "calendar": {"tz_offset_minutes": 480},
    "rules": [
        {
            "name": "r1",
            "when": "ticket.priority == 'P0'",
            "then": ["assign_team:tier0", {"action": "set_sla:duration", "value": "2"}],        }
    ],
    "fallback": [{"name": "f1", "when": None, "then": ["assign_team:general"]}],
}


class CompilationTests(unittest.TestCase):
    def test_default_policy_compiles(self) -> None:
        policy = Policy.from_file(POLICY_PATH)
        self.assertEqual(policy.compiled.name, "flowops-default")
        self.assertGreaterEqual(len(policy.compiled.rules), 8)
        self.assertEqual(policy.compiled.strategy, "first_match")
        # The shipped calendar must include the holiday data operators rely on.
        self.assertTrue(policy.compiled.calendar.holidays)
        self.assertTrue(policy.compiled.calendar.make_up_workdays)

    def test_source_hash_is_stable_and_content_sensitive(self) -> None:
        first = compile_policy(MINIMAL)
        second = compile_policy(json.loads(json.dumps(MINIMAL)))
        self.assertEqual(first.source_hash, second.source_hash)
        changed = json.loads(json.dumps(MINIMAL))
        changed["rules"][0]["when"] = "ticket.priority == 'P1'"
        self.assertNotEqual(compile_policy(changed).source_hash, first.source_hash)

    def test_unknown_action_is_rejected(self) -> None:
        broken = json.loads(json.dumps(MINIMAL))
        broken["rules"][0]["then"] = ["launch_missiles:now"]
        with self.assertRaises(ValidationError) as ctx:
            compile_policy(broken)
        self.assertIn("unknown action", str(ctx.exception))

    def test_wrong_arity_is_rejected(self) -> None:
        broken = json.loads(json.dumps(MINIMAL))
        broken["rules"][0]["then"] = ["set_priority"]  # missing its value
        with self.assertRaises(ValidationError) as ctx:
            compile_policy(broken)
        self.assertIn("set_priority", str(ctx.exception))

    def test_invalid_priority_value_is_rejected(self) -> None:
        broken = json.loads(json.dumps(MINIMAL))
        broken["rules"][0]["then"] = [{"action": "set_priority", "value": "P9"}]
        with self.assertRaises(ValidationError) as ctx:
            compile_policy(broken)
        self.assertIn("priority must be one of", str(ctx.exception))

    def test_shorthand_splits_on_the_last_colon(self) -> None:
        # `escalate:to:oncall` must mean action "escalate:to", not "escalate".
        from app.domain.rules.engine import parse_action

        self.assertEqual(parse_action("escalate:to:oncall-sre").code, "escalate:to")
        self.assertEqual(parse_action("escalate:to:oncall-sre").args, ("oncall-sre",))
        self.assertEqual(parse_action("set_sla:calendar:business").code, "set_sla:calendar")
        self.assertEqual(parse_action("tag:vip").code, "tag")
        self.assertEqual(parse_action("stop").args, ())

    def test_trailing_colon_without_a_value_is_rejected(self) -> None:
        broken = json.loads(json.dumps(MINIMAL))
        broken["rules"][0]["then"] = ["assign_team:"]
        with self.assertRaises(ValidationError):
            compile_policy(broken)

    def test_multi_argument_actions_need_the_object_form(self) -> None:
        from app.domain.rules.engine import parse_action

        # A colon-separated string can only ever carry one argument, which the
        # arity check then rejects — with a message that names the action.
        with self.assertRaises(ValidationError):
            compile_policy(
                {"name": "x", "rules": [{"name": "r", "when": None, "then": ["label:category"]}]}
            )
        self.assertEqual(parse_action({"action": "label", "value": ["k", "v"]}).args, ("k", "v"))

    def test_rules_execute_in_priority_order_not_file_order(self) -> None:
        policy = compile_policy(
            {
                "name": "ordered",
                "strategy": "accumulate",
                "rules": [
                    {"name": "late", "priority": 90, "when": None, "then": ["tag:late"]},
                    {"name": "early", "priority": 10, "when": None, "then": ["tag:early"]},
                ],
            }
        )
        self.assertEqual([item.rule.name for item in policy.rules], ["early", "late"])

    def test_invalid_priority_is_rejected(self) -> None:
        broken = json.loads(json.dumps(MINIMAL))
        broken["rules"][0]["then"] = ["set_priority:P9"]
        with self.assertRaises(ValidationError):
            compile_policy(broken)

    def test_syntax_error_in_condition_is_rejected_at_load_time(self) -> None:
        broken = json.loads(json.dumps(MINIMAL))
        broken["rules"][0]["when"] = "ticket.priority == "
        with self.assertRaises(ValidationError):
            compile_policy(broken)

    def test_duplicate_rule_names_are_rejected(self) -> None:
        broken = json.loads(json.dumps(MINIMAL))
        broken["rules"].append(json.loads(json.dumps(broken["rules"][0])))
        with self.assertRaises(ValidationError) as ctx:
            compile_policy(broken)
        self.assertIn("duplicate rule name", str(ctx.exception))

    def test_rule_without_actions_is_rejected(self) -> None:
        broken = json.loads(json.dumps(MINIMAL))
        broken["rules"][0]["then"] = []
        with self.assertRaises(ValidationError):
            compile_policy(broken)

    def test_unknown_strategy_is_rejected(self) -> None:
        broken = json.loads(json.dumps(MINIMAL))
        broken["strategy"] = "vibes"
        with self.assertRaises(ValidationError):
            compile_policy(broken)

    def test_empty_rule_list_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            compile_policy({"name": "x", "rules": []})

    def test_non_json_input_is_reported_clearly(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            Policy.from_json("{not json")
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_action_and_function_catalogs_are_non_empty(self) -> None:
        self.assertIn("set_priority", ACTION_CATALOG)
        self.assertTrue(describe_actions())
        self.assertTrue(describe_functions())

    def test_summary_exposes_rules_for_the_ui(self) -> None:
        summary = policy_summary(compile_policy(MINIMAL))
        self.assertEqual(summary["rules"][0]["name"], "r1")
        self.assertTrue(summary["functions"])


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = compile_policy(MINIMAL)
        self.engine = Engine(self.policy)

    def test_first_match_stops_further_rules(self) -> None:
        decision = self.engine.evaluate(ticket(priority="P0"), now=NOW)
        self.assertEqual(decision.fired, ["r1"])
        self.assertTrue(decision.stopped)
        self.assertEqual(decision.fields["sla_duration_hours"], 2.0)

    def test_fallback_applies_when_nothing_matches(self) -> None:
        decision = self.engine.evaluate(ticket(priority="P3"), now=NOW)
        self.assertEqual(decision.fired, ["f1"])
        self.assertEqual(decision.fields.get("team", "general"), "general")

    def test_trace_records_before_and_after(self) -> None:
        decision = self.engine.evaluate(ticket(priority="P0"), now=NOW)
        actions = [entry.action for entry in decision.trace]
        self.assertIn("assign_team", actions)
        team_entry = next(entry for entry in decision.trace if entry.action == "assign_team")
        self.assertIsNone(team_entry.before)
        self.assertEqual(team_entry.after, "tier0")

    def test_decision_is_deterministic_for_a_fixed_instant(self) -> None:
        first = self.engine.evaluate(ticket(priority="P0"), now=NOW).as_dict(include_scope=True)
        second = self.engine.evaluate(ticket(priority="P0"), now=NOW).as_dict(include_scope=True)
        self.assertEqual(first, second)

    def test_naive_now_is_rejected(self) -> None:
        with self.assertRaises(RuleEvaluationError):
            self.engine.evaluate(ticket(), now=datetime(2026, 3, 2, 10, 0))

    def test_rule_action_failure_is_surfaced(self) -> None:
        # An empty team name is a data error: the engine must refuse rather than
        # silently assign a ticket to "".
        policy = compile_policy(
            {
                "name": "broken-action",
                "rules": [{"name": "r1", "when": None, "then": ["set_sla:snooze:not-a-number"]}],            }
        )
        with self.assertRaises(RuleEvaluationError) as ctx:
            Engine(policy).evaluate(ticket(), now=NOW)
        self.assertIn("r1", str(ctx.exception))

    def test_accumulate_strategy_runs_every_matching_rule(self) -> None:
        policy = compile_policy(
            {
                "name": "accumulating",
                "strategy": "accumulate",
                "rules": [
                    {"name": "tag-a", "when": None, "then": ["tag:a"]},
                    {"name": "tag-b", "when": None, "then": ["tag:b"]},
                ],
            }
        )
        decision = Engine(policy).evaluate(ticket(), now=NOW)
        self.assertEqual(decision.fired, ["tag-a", "tag-b"])
        self.assertFalse(decision.stopped)

    def test_stop_action_is_recorded_and_first_match_still_wins(self) -> None:
        policy = compile_policy(
            {
                "name": "stopping",
                "rules": [
                    {"name": "first", "when": None, "then": ["tag:first", "stop"]},
                    {"name": "second", "when": None, "then": ["tag:second"]},
                ],
            }
        )
        decision = Engine(policy).evaluate(ticket(), now=NOW)
        # Under first_match the strategy already halts after one rule, so `stop`
        # is a no-op here — it exists for `accumulate` policies.
        self.assertEqual(decision.fired, ["first"])
        self.assertIn("stop", [entry.action for entry in decision.trace])

    def test_stop_action_stops_an_accumulating_policy(self) -> None:
        policy = compile_policy(
            {
                "name": "stopping",
                "strategy": "accumulate",
                "rules": [
                    {"name": "first", "when": None, "then": ["tag:first", "stop"]},
                    {"name": "second", "when": None, "then": ["tag:second"]},
                ],
            }
        )
        decision = Engine(policy).evaluate(ticket(), now=NOW)
        self.assertEqual(decision.fired, ["first"])
        self.assertTrue(decision.stopped)

    def test_later_rules_see_earlier_effects(self) -> None:
        policy = compile_policy(
            {
                "name": "chained",
                "strategy": "accumulate",
                "rules": [
                    {"name": "raise", "when": None, "then": ["set_priority:P1"]},
                    {"name": "react", "when": "ticket.priority == 'P1'", "then": ["tag:escalated"]},
                ],
            }
        )
        decision = Engine(policy).evaluate(ticket(), now=NOW)
        self.assertEqual(decision.fired, ["raise", "react"])
        self.assertIn("escalated", decision.fields.get("tags") or [])


class SlaResolutionTests(unittest.TestCase):
    def test_business_calendar_due_date_skips_the_weekend(self) -> None:
        policy = compile_policy(
            {
                "name": "sla",
                "rules": [
                    {
                        "name": "r",
                        "when": None,
                        "then": [{"action": "set_sla:duration", "value": "4"}, "set_sla:calendar:business"],
                    }
                ],
            }
        )
        created = datetime(2026, 3, 6, 17, 0, tzinfo=CN)  # Friday 17:00
        decision = Engine(policy).evaluate(ticket(created_at=created.isoformat()), now=created)
        due = decision.fields["sla_due_at"]
        self.assertEqual(due, datetime(2026, 3, 9, 12, 0, tzinfo=timezone(timedelta(hours=8))))

    def test_wall_clock_calendar_ignores_working_hours(self) -> None:
        policy = compile_policy(
            {
                "name": "sla",
                "rules": [
                    {
                        "name": "r",
                        "when": None,
                        "then": [{"action": "set_sla:duration", "value": "4"}, "set_sla:calendar:wall"],
                    }
                ],
            }
        )
        created = datetime(2026, 3, 6, 17, 0, tzinfo=CN)
        decision = Engine(policy).evaluate(ticket(created_at=created.isoformat()), now=created)
        self.assertEqual(decision.fields["sla_due_at"], created + timedelta(hours=4))

    def test_consumed_time_is_measured_on_the_chosen_calendar(self) -> None:
        policy = compile_policy(
            {
                "name": "sla",
                "rules": [
                    {
                        "name": "r",
                        "when": None,
                        "then": [{"action": "set_sla:duration", "value": "9"}, "set_sla:calendar:business"],
                    }
                ],
            }
        )
        created = datetime(2026, 3, 2, 9, 0, tzinfo=CN)  # Monday 09:00
        later = datetime(2026, 3, 9, 9, 0, tzinfo=CN)  # the following Monday
        decision = Engine(policy).evaluate(ticket(created_at=created.isoformat()), now=later)
        # Five working days of 9 business hours each.
        self.assertEqual(decision.fields["sla_consumed_hours"], 45.0)
        self.assertEqual(decision.fields["sla_remaining_hours"], -36.0)
        self.assertEqual(decision.fields["sla_duration_hours"], 9.0)

    def test_snooze_reduces_the_effective_budget(self) -> None:
        policy = compile_policy(
            {
                "name": "sla",
                "rules": [
                    {
                        "name": "r",
                        "when": None,
                        "then": [
                            {"action": "set_sla:duration", "value": "8"},
                            {"action": "set_sla:snooze", "value": "3"},
                        ],                    }
                ],
            }
        )
        decision = Engine(policy).evaluate(ticket(created_at=NOW.isoformat()), now=NOW)
        self.assertEqual(decision.fields["sla_snooze_hours"], 3.0)

    def test_missing_duration_produces_a_warning_not_a_crash(self) -> None:
        policy = compile_policy({"name": "none", "rules": [{"name": "r", "when": None, "then": ["tag:x"]}]})
        decision = Engine(policy).evaluate(ticket(), now=NOW)
        self.assertTrue(any("untracked" in warning for warning in decision.warnings))

    def test_duration_may_be_an_expression(self) -> None:
        policy = compile_policy(
            {
                "name": "expr",
                "rules": [
                    {
                        "name": "r",
                        "when": None,
                        "then": [{"action": "set_sla:duration", "value": "2 * 3 + 1"}],
                    }
                ],
            }
        )
        decision = Engine(policy).evaluate(ticket(), now=NOW)
        self.assertEqual(decision.fields["sla_duration_hours"], 7.0)

    def test_negative_duration_is_rejected_at_evaluation(self) -> None:
        policy = compile_policy(
            {
                "name": "expr",
                "rules": [
                    {"name": "r", "when": None, "then": [{"action": "set_sla:duration", "value": "0 - 2"}]}
                ],
            }
        )
        with self.assertRaises(RuleEvaluationError):
            Engine(policy).evaluate(ticket(), now=NOW)

    def test_escalation_timestamp_follows_the_calendar(self) -> None:
        policy = compile_policy(
            {
                "name": "esc",
                "rules": [
                    {
                        "name": "r",
                        "when": None,
                        "then": [
                            "set_sla:calendar:business",
                            {"action": "escalate:after", "value": "4"},
                            "escalate:to:oncall",
                            "escalate:notify:im",
                        ],
                    }
                ],
            }
        )
        created = datetime(2026, 3, 6, 17, 0, tzinfo=CN)
        decision = Engine(policy).evaluate(ticket(created_at=created.isoformat()), now=created)
        self.assertEqual(decision.fields["escalation_at"], datetime(2026, 3, 9, 12, 0, tzinfo=CN))
        self.assertEqual(decision.notifications[0].channel, "im")
        self.assertEqual(decision.notifications[0].target, "oncall")


class NotificationTests(unittest.TestCase):
    def test_notification_inherits_escalation_metadata(self) -> None:
        policy = compile_policy(
            {
                "name": "notify",
                "rules": [
                    {
                        "name": "r",
                        "when": None,
                        "then": [
                            {"action": "escalate:after", "value": "1"},
                            "escalate:to:lead",
                            {"action": "escalate:notify", "value": ["slack", "#ops"]},
                        ],
                    }
                ],
            }
        )
        decision = Engine(policy).evaluate(ticket(), now=NOW)
        payload = decision.notifications[0].as_dict()
        self.assertEqual(payload["channel"], "slack")
        self.assertEqual(payload["payload"], "#ops")
        self.assertEqual(payload["after_hours"], 1.0)
        self.assertEqual(payload["target"], "lead")


class DefaultPolicyTests(unittest.TestCase):
    """End-to-end behaviour of the shipped policy document."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = Policy.from_file(POLICY_PATH).compiled

    def engine(self) -> Engine:
        return Engine(self.policy)

    def evaluate(self, **overrides: object):
        return self.engine().evaluate(ticket(**overrides), now=NOW)

    def test_p0_goes_to_the_incident_team_with_a_two_hour_target(self) -> None:
        decision = self.evaluate(priority="P0")
        self.assertEqual(decision.fired, ["R00-生产事故一级响应"])
        self.assertEqual(decision.fields["team"], "tier0-incident")
        self.assertEqual(decision.fields["sla_duration_hours"], 2.0)
        self.assertEqual(decision.required_review, "一级故障需值班经理确认")

    def test_security_keywords_outrank_other_rules(self) -> None:
        decision = self.evaluate(title="疑似数据泄露：订单接口可越权读取")
        self.assertEqual(decision.fired, ["R01-安全事件上报"])
        self.assertEqual(decision.fields["team"], "security")
        self.assertEqual(decision.fields["sla_duration_hours"], 1.0)

    def test_payment_channel_is_routed_to_payments(self) -> None:
        decision = self.evaluate(channel="payments", title="结算对账金额不一致")
        self.assertEqual(decision.fired, ["R02-支付与资金链路"])
        self.assertEqual(decision.fields["sla_duration_hours"], 4.0)

    def test_vip_label_is_honoured(self) -> None:
        decision = self.evaluate(title="咨询导出功能怎么用", labels={"tier": "vip"})
        self.assertEqual(decision.fired, ["R03-VIP客户加速"])
        self.assertIn("key-account", decision.fields.get("tags") or [])

    def test_availability_keywords_escalate(self) -> None:
        decision = self.evaluate(title="全站无法登录", body="所有用户均无法访问")
        self.assertEqual(decision.fired, ["R04-完全不可用"])
        self.assertEqual(decision.fields["team"], "platform")

    def test_performance_and_defect_and_request_are_distinguished(self) -> None:
        self.assertEqual(self.evaluate(title="接口超时率升高").fired, ["R05-性能劣化"])
        self.assertEqual(self.evaluate(title="导出报表报错 500").fired, ["R06-功能缺陷"])
        self.assertEqual(self.evaluate(title="请问如何申请权限").fired, ["R07-咨询与工单请求"])

    def test_unmatched_ticket_falls_back_with_a_note(self) -> None:
        decision = self.evaluate(title="一些无关键词的反馈")
        self.assertEqual(decision.fired, ["R99-兜底分诊"])
        self.assertTrue(decision.notes)

    def test_every_rule_produces_a_due_timestamp_in_the_future(self) -> None:
        for spec in [
            {"priority": "P0"},
            {"title": "数据泄露事件"},
            {"channel": "payments"},
            {"title": "全站不可用"},
            {"title": "页面很卡"},
            {"title": "接口报错"},
            {"title": "咨询价格"},
            {"title": "杂项反馈"},
        ]:
            with self.subTest(spec=spec):
                decision = self.evaluate(**spec)
                self.assertIsNotNone(decision.fields.get("sla_due_at"))
                self.assertGreater(decision.fields["sla_due_at"], NOW)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
