"""Use-case tests: idempotency, optimistic concurrency, audit and pagination."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.adapters.persistence.memory import InMemoryStore
from app.application.models import TicketQuery
from app.application.services.tickets import ListEventSink, TicketService
from app.domain.errors import (
    ConflictError,
    IllegalTransitionError,
    NotFoundError,
    PreconditionFailedError,
    ValidationError,
)
from app.domain.rules.engine import Policy
from app.domain.values import Priority, RiskBand, Status

CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 3, 2, 10, 0, tzinfo=CN)  # Monday 10:00 local
POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "policies" / "default.json"


class TestClock:
    """Deterministic clock so SLA arithmetic in tests is reproducible."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment

    def advance(self, **kwargs: float) -> None:
        self.moment = self.moment + timedelta(**kwargs)


class SequentialIds:
    def __init__(self) -> None:
        self.index = 0

    def ticket_id(self) -> str:
        self.index += 1
        return f"TCK-TEST-{self.index:04d}"


def build() -> tuple[TicketService, InMemoryStore, TestClock, ListEventSink]:
    policy = Policy.from_file(POLICY_PATH).compiled
    store = InMemoryStore()
    clock = TestClock(NOW)
    events = ListEventSink()
    service = TicketService(store, policy, clock=clock, ids=SequentialIds(), events=events)
    return service, store, clock, events


class CreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.store, self.clock, self.events = build()

    def test_creation_triages_the_ticket(self) -> None:
        ticket, decision, replayed = self.service.create(
            {"title": "全站无法登录", "body": "所有用户白屏"}, now=NOW
        )
        self.assertFalse(replayed)
        self.assertIsNotNone(decision)
        self.assertEqual(ticket.status, Status.TRIAGED)
        self.assertEqual(ticket.team, "platform")
        self.assertEqual(ticket.priority, Priority.P1)
        self.assertEqual(ticket.sla_duration_hours, 4.0)
        self.assertIsNotNone(ticket.sla_due_at)
        self.assertEqual(ticket.version, 1)

    def test_p0_creation_requires_review_and_records_a_deadline(self) -> None:
        ticket, decision, _ = self.service.create(
            {"title": "核心链路不可用", "priority": "P0"}, now=NOW
        )
        self.assertIsNotNone(decision)
        self.assertEqual(ticket.priority, Priority.P0)
        self.assertIsNotNone(ticket.required_review)
        self.assertEqual(ticket.sla_duration_hours, 2.0)

    def test_validation_rejects_an_empty_title(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.create({"title": "   "}, now=NOW)

    def test_events_are_emitted_for_observability(self) -> None:
        self.service.create({"title": "咨询一个问题"}, now=NOW)
        self.assertIn("ticket.created", self.events.names())

    def test_idempotent_replay_returns_the_original_ticket(self) -> None:
        payload = {"title": "导出报表报错"}
        first, _, replayed = self.service.create(payload, idempotency_key="key-00000001", now=NOW)
        second, decision, replayed_again = self.service.create(
            payload, idempotency_key="key-00000001", now=NOW
        )
        self.assertFalse(replayed)
        self.assertTrue(replayed_again)
        self.assertEqual(first.id, second.id)
        self.assertIsNone(decision)
        self.assertEqual(len(self.store.all_tickets()), 1)

    def test_same_key_with_a_different_body_is_a_conflict(self) -> None:
        self.service.create({"title": "第一个标题"}, idempotency_key="key-00000002", now=NOW)
        with self.assertRaises(ConflictError) as ctx:
            self.service.create({"title": "完全不同的标题"}, idempotency_key="key-00000002", now=NOW)
        self.assertEqual(ctx.exception.status, 409)

    def test_audit_trail_records_creation_and_policy_decision(self) -> None:
        ticket, _, _ = self.service.create({"title": "支付失败无法下单"}, now=NOW)
        trail = self.service.trail(ticket.id)
        actions = [entry.action for entry in trail]
        self.assertEqual(actions, ["ticket.created", "policy.evaluated"])
        self.assertEqual([entry.seq for entry in trail], [1, 2])
        policy_entry = trail[1]
        self.assertTrue(policy_entry.meta["rules_fired"])
        self.assertTrue(policy_entry.meta["trace"])

    def test_preview_does_not_persist(self) -> None:
        decision = self.service.preview({"title": "数据泄露风险"}, now=NOW)
        self.assertEqual(decision.fired, ["R01-安全事件上报"])
        self.assertEqual(len(self.store.all_tickets()), 0)
        self.assertIn("ticket", decision.scope)

    def test_backdated_creation_consumes_business_time(self) -> None:
        # Created Monday 04:00 local; by 10:00 only 09:00-10:00 counts (1 business
        # hour), which is exactly what a business-hours SLA is supposed to do.
        ticket, _, _ = self.service.create(
            {"title": "支付对账不一致", "created_at": (NOW - timedelta(hours=6)).isoformat()},
            now=NOW,
        )
        self.assertIsNotNone(ticket.sla_consumed_hours)
        self.assertAlmostEqual(ticket.sla_consumed_hours or 0, 1.0, places=3)
        self.assertGreater(ticket.sla_consumed_hours or 0, 0.0)
        self.assertEqual(ticket.risk(), RiskBand.HEALTHY)

    def test_a_breached_ticket_reports_the_breached_band(self) -> None:
        # Opened Saturday 2026-02-28 at 15:00 local.  That Saturday is a
        # published 春节调休 make-up workday, so it *does* consume SLA time;
        # the following Sunday does not.  Monday 10:00 therefore sees
        # 3 business hours (Sat) + 1 (Mon) = 4 of the 4-hour budget, so the
        # ticket is exactly at its limit and due at 13:00.
        created = datetime(2026, 2, 28, 15, 0, tzinfo=CN)
        ticket, _, _ = self.service.create(
            {"title": "支付退款延迟", "created_at": created.isoformat()}, now=NOW
        )
        self.assertEqual(ticket.sla_duration_hours, 4.0)
        self.assertEqual(ticket.sla_consumed_hours, 4.0)
        self.assertEqual(ticket.risk(), RiskBand.BREACHED)
        # At the limit but not yet past it: the deadline still has to lapse.
        self.assertFalse(ticket.is_breached(NOW))
        self.assertTrue(ticket.is_breached(datetime(2026, 3, 2, 14, 0, tzinfo=CN)))

    def test_an_ordinary_weekend_contributes_no_sla_time(self) -> None:
        # Saturday 2026-03-07 is a plain weekend day, so a ticket opened then is
        # untouched when Monday 09:00 comes around.
        saturday = datetime(2026, 3, 7, 15, 0, tzinfo=CN)
        ticket, _, _ = self.service.create(
            {"title": "支付退款延迟", "created_at": saturday.isoformat()},
            now=datetime(2026, 3, 9, 9, 0, tzinfo=CN),
        )
        self.assertEqual(ticket.sla_consumed_hours, 0.0)
        self.assertEqual(ticket.risk(), RiskBand.HEALTHY)


class UpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.store, self.clock, _ = build()
        self.ticket, _, _ = self.service.create({"title": "接口偶发报错"}, now=NOW)

    def test_patch_changes_a_field_and_bumps_the_version(self) -> None:
        updated = self.service.update(
            self.ticket.id, {"assignee": "app-张伟"}, expected_version=1, now=NOW
        )
        self.assertEqual(updated.assignee, "app-张伟")
        self.assertEqual(updated.version, 2)

    def test_stale_version_is_rejected(self) -> None:
        self.service.update(self.ticket.id, {"assignee": "a"}, expected_version=1, now=NOW)
        with self.assertRaises(PreconditionFailedError) as ctx:
            self.service.update(self.ticket.id, {"assignee": "b"}, expected_version=1, now=NOW)
        self.assertEqual(ctx.exception.status, 412)
        self.assertEqual(ctx.exception.extra["current_version"], 2)

    def test_unknown_field_is_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.service.update(self.ticket.id, {"status": "closed"}, now=NOW)
        self.assertIn("cannot be patched", str(ctx.exception))

    def test_invalid_priority_value_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.update(self.ticket.id, {"priority": "P9"}, now=NOW)

    def test_tags_are_deduplicated_and_trimmed(self) -> None:
        updated = self.service.update(
            self.ticket.id, {"tags": [" a ", "a", "b", ""]}, now=NOW
        )
        self.assertEqual(list(updated.tags), ["a", "b"])

    def test_snooze_shifts_the_due_timestamp(self) -> None:
        before = self.ticket.sla_due_at
        updated = self.service.update(self.ticket.id, {"sla_snooze_hours": 2}, now=NOW)
        self.assertEqual(updated.sla_snooze_hours, 2.0)
        self.assertNotEqual(updated.sla_due_at, before)

    def test_negative_snooze_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.update(self.ticket.id, {"sla_snooze_hours": -1}, now=NOW)

    def test_no_op_update_returns_the_same_version(self) -> None:
        same = self.service.update(self.ticket.id, {"assignee": None}, now=NOW)
        self.assertEqual(same.version, self.ticket.version)

    def test_update_unknown_ticket_is_a_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.update("TCK-NOPE", {"assignee": "x"}, now=NOW)


class TransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.store, self.clock, _ = build()
        self.ticket, _, _ = self.service.create({"title": "咨询权限申请流程"}, now=NOW)

    def test_legal_transition_succeeds_and_is_audited(self) -> None:
        moved = self.service.transition(self.ticket.id, "in_progress", note="开始处理", now=NOW)
        self.assertEqual(moved.status, Status.IN_PROGRESS)
        trail = self.service.trail(self.ticket.id)
        self.assertEqual(trail[-1].action, "ticket.transitioned")
        self.assertEqual(trail[-1].meta["note"], "开始处理")

    def test_illegal_transition_is_rejected_with_the_allowed_set(self) -> None:
        self.service.transition(self.ticket.id, "in_progress", now=NOW)
        with self.assertRaises(IllegalTransitionError) as ctx:
            self.service.transition(self.ticket.id, "closed", now=NOW)
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("blocked", ctx.exception.extra["allowed"])
        self.assertNotIn("closed", ctx.exception.extra["allowed"])

    def test_closing_requires_resolving_first(self) -> None:
        self.service.transition(self.ticket.id, "in_progress", now=NOW)
        self.service.transition(self.ticket.id, "resolved", now=NOW)
        closed = self.service.transition(self.ticket.id, "closed", now=NOW)
        self.assertEqual(closed.status, Status.CLOSED)

    def test_resolving_stamps_closed_at_and_freezes_the_clock(self) -> None:
        self.service.transition(self.ticket.id, "in_progress", now=NOW)
        resolved = self.service.transition(self.ticket.id, "resolved", now=NOW)
        self.assertIsNotNone(resolved.closed_at)
        frozen = resolved.sla_consumed_hours
        self.clock.advance(hours=5)
        later = self.service.get(self.ticket.id)
        self.assertEqual(later.sla_consumed_hours, frozen)

    def test_reopening_clears_closed_at(self) -> None:
        self.service.transition(self.ticket.id, "in_progress", now=NOW)
        self.service.transition(self.ticket.id, "resolved", now=NOW)
        reopened = self.service.transition(self.ticket.id, "in_progress", now=NOW)
        self.assertIsNone(reopened.closed_at)

    def test_unknown_status_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.transition(self.ticket.id, "finished", now=NOW)

    def test_stale_version_blocks_the_transition(self) -> None:
        self.service.update(self.ticket.id, {"assignee": "x"}, now=NOW)
        with self.assertRaises(PreconditionFailedError):
            self.service.transition(self.ticket.id, "in_progress", expected_version=1, now=NOW)


class QueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.store, self.clock, _ = build()
        specs = [
            ("全站不可用，所有用户无法登录", 5),
            ("支付退款延迟", 9),
            ("页面卡顿超时", 5),
            ("咨询发票开具", 3),
            ("导出报错 500", 1),
        ]
        for title, hours in specs:
            self.service.create(
                {"title": title, "created_at": (NOW - timedelta(hours=hours)).isoformat()}, now=NOW
            )
        # One deliberately breached ticket so the risk filter has something to find.
        self.service.create(
            {
                "title": "支付对账严重不一致",
                "created_at": datetime(2026, 2, 28, 9, 0, tzinfo=CN).isoformat(),
            },
            now=NOW,
        )

    def test_default_sort_is_newest_first(self) -> None:
        page = self.service.list(TicketQuery(limit=10))
        created = [ticket.created_at for ticket in page.items]
        self.assertEqual(created, sorted(created, reverse=True))
        self.assertEqual(page.total, 6)
        self.assertFalse(page.has_more)

    def test_cursor_pagination_walks_without_gaps_or_repeats(self) -> None:
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page = self.service.list(TicketQuery(limit=2, cursor=cursor))
            seen.extend(ticket.id for ticket in page.items)
            cursor = page.next_cursor
            if not cursor:
                break
        self.assertEqual(len(seen), 6)
        self.assertEqual(len(set(seen)), 6)

    def test_cursor_from_different_filters_is_rejected(self) -> None:
        page = self.service.list(TicketQuery(limit=2))
        with self.assertRaises(ValidationError):
            self.service.list(TicketQuery(limit=2, cursor=page.next_cursor, team="payments"))

    def test_malformed_cursor_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.list(TicketQuery(cursor="not-a-cursor"))

    def test_filter_by_status_and_priority(self) -> None:
        by_priority = self.service.list(TicketQuery(priority=["P1"], limit=10))
        self.assertTrue(all(ticket.priority is Priority.P1 for ticket in by_priority.items))
        by_status = self.service.list(TicketQuery(status=["triaged"], limit=10))
        self.assertTrue(all(ticket.status is Status.TRIAGED for ticket in by_status.items))

    def test_filter_by_risk_band(self) -> None:
        risky = self.service.list(TicketQuery(risk=["breached", "critical"], limit=10))
        self.assertTrue(all(ticket.risk() in (RiskBand.BREACHED, RiskBand.CRITICAL) for ticket in risky.items))
        self.assertGreater(len(risky.items), 0)

    def test_search_matches_title_body_and_id(self) -> None:
        found = self.service.list(TicketQuery(search="退款", limit=10))
        self.assertEqual(len(found.items), 1)
        self.assertIn("退款", found.items[0].title)

    def test_sort_by_priority_is_rank_ordered(self) -> None:
        page = self.service.list(TicketQuery(sort="priority", limit=10))
        ranks = [ticket.priority.rank for ticket in page.items]
        self.assertEqual(ranks, sorted(ranks))

    def test_limit_bounds_are_enforced(self) -> None:
        for bad in (0, 201):
            with self.assertRaises(ValidationError):
                self.service.list(TicketQuery(limit=bad))

    def test_unknown_sort_field_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.list(TicketQuery(sort="title"))


class OperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.store, self.clock, _ = build()
        # Opened Saturday 2026-02-28 15:00 local (a 春节调休 workday): by Monday
        # 10:00 the clock has consumed four hours of a four-hour - actually a
        # four-hour budget is exactly exhausted, leaving the ticket inside the
        # "due soon" horizon.
        created = datetime(2026, 2, 28, 15, 0, tzinfo=CN)
        self.service.create({"title": "全站不可用", "created_at": created.isoformat()}, now=NOW)
        self.service.create({"title": "咨询发票开具"}, now=NOW)
        # Recompute the clocks first: a sweep is what makes the derived risk band
        # (and therefore any risk-based query) reflect the current instant.
        self.service.refresh_sla(now=NOW)

    def test_snapshot_reports_totals_and_policy_identity(self) -> None:
        snapshot = self.service.sla_snapshot(now=NOW)
        self.assertEqual(snapshot["totals"]["all"], 2)
        self.assertEqual(snapshot["totals"]["open"], 2)
        self.assertGreaterEqual(snapshot["totals"]["due_soon"], 1)
        self.assertEqual(snapshot["policy"]["name"], "flowops-default")
        self.assertIn("Mon", snapshot["calendar"])

    def test_a_freshly_triaged_ticket_is_healthy(self) -> None:
        page = self.service.list(TicketQuery(risk=["healthy"], limit=10))
        self.assertEqual(len(page.items), 1)
        self.assertIn("咨询", page.items[0].title)

    def test_refresh_recomputes_consumed_time_without_retriaging(self) -> None:
        ticket = self.store.all_tickets()[0]
        before_priority, before_duration = ticket.priority, ticket.sla_duration_hours
        self.clock.advance(hours=3)
        result = self.service.refresh_sla(now=self.clock.now())
        self.assertGreaterEqual(result["tickets_touched"], 1)
        after = self.service.get(ticket.id)
        self.assertEqual(after.priority, before_priority)
        self.assertEqual(after.sla_duration_hours, before_duration)
        self.assertGreater(after.sla_consumed_hours or 0, ticket.sla_consumed_hours or 0)

    def test_risk_histogram_covers_every_band(self) -> None:
        histogram = self.service.risk_histogram(now=NOW)
        self.assertEqual(set(histogram), {band.value for band in RiskBand})
        self.assertEqual(sum(histogram.values()), 2)

    def test_trail_for_unknown_ticket_is_a_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.trail("TCK-MISSING")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
