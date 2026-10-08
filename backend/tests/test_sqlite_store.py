"""SQLite adapter tests.

The in-memory store is not a substitute for these: an UPDATE with the wrong
number of bindings passes every memory-backed test and fails on the first real
write.  Anything that touches SQL is verified here against a real database file.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.adapters.persistence.sqlite_store import SqliteStore
from app.application.models import AuditEntry, IdempotencyRecord, TicketQuery
from app.application.services.tickets import TicketService
from app.bootstrap import load_policy
from app.config.settings import BACKEND_ROOT, Settings
from app.domain.errors import ConflictError, NotFoundError, PreconditionFailedError, ValidationError
from app.domain.rules.engine import Policy
from app.domain.ticket import Ticket
from app.domain.values import Priority, Status

CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 3, 2, 10, 0, tzinfo=CN)
POLICY_PATH = BACKEND_ROOT / "config" / "policies" / "default.json"


class Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment


class SqliteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "flowops-test.db"
        self.store = SqliteStore(self.path)
        self.policy = Policy.from_file(POLICY_PATH).compiled

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def make_ticket(self, ticket_id: str = "TCK-1", **overrides: object) -> Ticket:
        defaults = {
            "ticket_id": ticket_id,
            "title": "示例工单",
            "body": "内容",
            "reporter": "tester",
            "channel": "web",
            "created_at": NOW,
        }
        defaults.update(overrides)
        return Ticket.create(**defaults)  # type: ignore[arg-type]

    # -- CRUD -----------------------------------------------------------
    def test_round_trip_preserves_every_field(self) -> None:
        ticket = self.make_ticket(
            tags=("a", "b"),
            labels={"env": "prod", "tier": "vip"},
            priority=Priority.P1,
        )
        self.store.add(ticket)
        loaded = self.store.get("TCK-1")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.title, ticket.title)
        self.assertEqual(list(loaded.tags), ["a", "b"])
        self.assertEqual(dict(loaded.labels), {"env": "prod", "tier": "vip"})
        self.assertEqual(loaded.priority, Priority.P1)
        self.assertEqual(loaded.status, Status.OPEN)
        self.assertEqual(loaded.created_at, ticket.created_at)

    def test_duplicate_id_is_a_conflict(self) -> None:
        self.store.add(self.make_ticket())
        with self.assertRaises(ConflictError):
            self.store.add(self.make_ticket())

    def test_get_unknown_returns_none(self) -> None:
        self.assertIsNone(self.store.get("TCK-NOPE"))

    def test_save_updates_the_row_and_bumps_the_version(self) -> None:
        # Regression: the UPDATE statement must bind exactly as many values as it
        # has placeholders, and must not try to write the primary key.
        self.store.add(self.make_ticket())
        ticket = self.store.get("TCK-1")
        assert ticket is not None
        updated = self.store.save(ticket.with_updates(assignee="张伟", sla_duration_hours=8.0))
        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.assignee, "张伟")
        reloaded = self.store.get("TCK-1")
        assert reloaded is not None
        self.assertEqual(reloaded.assignee, "张伟")
        self.assertEqual(reloaded.version, 2)
        self.assertEqual(reloaded.sla_duration_hours, 8.0)

    def test_save_persists_datetime_columns(self) -> None:
        self.store.add(self.make_ticket())
        ticket = self.store.get("TCK-1")
        assert ticket is not None
        due = NOW + timedelta(hours=4)
        closed = NOW + timedelta(hours=6)
        self.store.save(ticket.with_updates(sla_due_at=due, closed_at=closed, status=Status.RESOLVED))
        reloaded = self.store.get("TCK-1")
        assert reloaded is not None
        self.assertEqual(reloaded.sla_due_at, due)
        self.assertEqual(reloaded.closed_at, closed)
        self.assertEqual(reloaded.status, Status.RESOLVED)

    def test_stale_expected_version_is_rejected(self) -> None:
        self.store.add(self.make_ticket())
        ticket = self.store.get("TCK-1")
        assert ticket is not None
        self.store.save(ticket.with_updates(assignee="a"), expected_version=1)
        with self.assertRaises(PreconditionFailedError):
            self.store.save(ticket.with_updates(assignee="b"), expected_version=1)

    def test_save_unknown_ticket_is_a_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.store.save(self.make_ticket("TCK-MISSING"))

    # -- audit ----------------------------------------------------------
    def test_audit_sequence_is_monotonic_and_ordered(self) -> None:
        self.store.add(self.make_ticket())
        for index in range(3):
            self.store.append_audit(
                AuditEntry(
                    seq=self.store.next_audit_seq("TCK-1"),
                    ticket_id="TCK-1",
                    action=f"step-{index}",
                    actor="tester",
                    at=NOW,
                    changes={"index": index},
                    meta={"note": "中文备注"},
                )
            )
        trail = self.store.audit_trail("TCK-1")
        self.assertEqual([entry.seq for entry in trail], [1, 2, 3])
        self.assertEqual(trail[2].changes["index"], 2)
        self.assertEqual(trail[0].meta["note"], "中文备注")

    def test_idempotency_records_round_trip(self) -> None:
        record = IdempotencyRecord(
            key="key-12345678",
            ticket_id="TCK-1",
            request_fingerprint="abc",
            status=201,
            body={"ticket": {"id": "TCK-1", "标题": "中文"}},
            created_at=NOW,
        )
        self.store.remember_idempotent(record)
        found = self.store.find_idempotent("key-12345678")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.body["ticket"]["标题"], "中文")
        self.assertIsNone(self.store.find_idempotent("other"))

    def test_remember_idempotent_is_insert_or_ignore(self) -> None:
        first = IdempotencyRecord("k-00000001", "TCK-1", "f1", 201, {"v": 1}, NOW)
        second = IdempotencyRecord("k-00000001", "TCK-2", "f2", 201, {"v": 2}, NOW)
        self.store.remember_idempotent(first)
        self.store.remember_idempotent(second)
        found = self.store.find_idempotent("k-00000001")
        assert found is not None
        self.assertEqual(found.ticket_id, "TCK-1")
        self.assertEqual(found.body, {"v": 1})

    # -- queries --------------------------------------------------------
    def test_query_filters_sorts_and_paginates(self) -> None:
        for index in range(5):
            self.store.add(
                self.make_ticket(
                    f"TCK-{index}",
                    title=f"工单 {index}",
                    created_at=NOW - timedelta(hours=index),
                )
            )
        page = self.store.query(TicketQuery(limit=2))
        self.assertEqual(page.total, 5)
        self.assertEqual(len(page.items), 2)
        self.assertTrue(page.has_more)
        self.assertIsNotNone(page.next_cursor)

        second = self.store.query(TicketQuery(limit=2, cursor=page.next_cursor))
        self.assertEqual(len(second.items), 2)
        first_ids = {ticket.id for ticket in page.items}
        self.assertEqual(first_ids & {ticket.id for ticket in second.items}, set())

    def test_query_search_and_status_filter(self) -> None:
        self.store.add(self.make_ticket("TCK-1", title="支付网关 502"))
        self.store.add(self.make_ticket("TCK-2", title="咨询发票"))
        found = self.store.query(TicketQuery(search="支付"))
        self.assertEqual([ticket.id for ticket in found.items], ["TCK-1"])

    def test_invalid_sort_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.query(TicketQuery(sort="; DROP TABLE tickets"))

    def test_counts_and_size_helpers(self) -> None:
        self.store.add(self.make_ticket())
        counts = self.store.table_counts()
        self.assertEqual(counts["tickets"], 1)
        self.assertGreater(self.store.size_bytes(), 0)
        self.store.set_schema_version("3")
        self.assertEqual(self.store.schema_version(), "3")


class SqliteServiceIntegrationTests(unittest.TestCase):
    """The full use-case stack over a real file-backed store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "flowops.db"
        settings = Settings(
            store="sqlite",
            database=self.path,
            policy_path=POLICY_PATH,
            calendar_path=BACKEND_ROOT / "config" / "calendars" / "cn-holidays.json",
        )
        policy, _document = load_policy(settings)
        self.store = SqliteStore(self.path)
        self.service = TicketService(self.store, policy.compiled, clock=Clock(NOW))

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_create_then_transition_then_reopen_persists_across_reads(self) -> None:
        ticket, decision, _ = self.service.create(
            {
                "title": "退款到账延迟，用户投诉",
                "created_at": (NOW - timedelta(hours=3)).isoformat(),
            },
            now=NOW,
        )
        self.assertIsNotNone(decision)
        self.assertEqual(ticket.team, "payments")

        moved = self.service.transition(ticket.id, "in_progress", note="开始排查", now=NOW)
        self.assertEqual(moved.status, Status.IN_PROGRESS)
        resolved = self.service.transition(ticket.id, "resolved", now=NOW)
        self.assertIsNotNone(resolved.closed_at)
        reopened = self.service.transition(ticket.id, "in_progress", now=NOW)
        self.assertIsNone(reopened.closed_at)
        # create(1) -> in_progress(2) -> resolved(3) -> in_progress(4)
        self.assertEqual(reopened.version, 4)

        trail = self.service.trail(ticket.id)
        self.assertEqual([entry.seq for entry in trail], list(range(1, len(trail) + 1)))
        self.assertIn("ticket.transitioned", [entry.action for entry in trail])

    def test_idempotent_create_survives_a_restart(self) -> None:
        payload = {"title": "咨询发票开具"}
        first, _, _ = self.service.create(payload, idempotency_key="key-abcdefgh", now=NOW)
        self.store.close()

        reopened = SqliteStore(self.path)
        try:
            policy = Policy.from_file(POLICY_PATH).compiled
            service = TicketService(reopened, policy, clock=Clock(NOW))
            second, decision, replayed = service.create(
                payload, idempotency_key="key-abcdefgh", now=NOW
            )
            self.assertTrue(replayed)
            self.assertIsNone(decision)
            self.assertEqual(first.id, second.id)
            self.assertEqual(len(reopened.all_tickets()), 1)
        finally:
            reopened.close()
            self.store = SqliteStore(self.path)  # keep tearDown valid

    def test_refresh_sla_updates_rows_in_place(self) -> None:
        ticket, _, _ = self.service.create({"title": "页面卡顿超时"}, now=NOW)
        before = self.service.get(ticket.id).sla_consumed_hours
        later = NOW + timedelta(hours=3)
        service = TicketService(self.store, Policy.from_file(POLICY_PATH).compiled, clock=Clock(later))
        result = service.refresh_sla(now=later)
        self.assertGreaterEqual(result["tickets_touched"], 1)
        after = service.get(ticket.id)
        self.assertGreater(after.sla_consumed_hours or 0, before or 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
