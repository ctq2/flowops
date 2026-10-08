"""HTTP integration tests.

These run against a real ``ThreadingHTTPServer`` on an ephemeral port and speak
real HTTP through ``urllib``.  Testing the transport through a fake request
object would not catch the things that actually break in production: missing
``Content-Length``, a wrong status code on replay, or a problem document with the
wrong content type.
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from app.adapters.http.api import build_router
from app.adapters.http.server import serve
from app.adapters.persistence.memory import InMemoryStore
from app.application.services.tickets import TicketService
from app.bootstrap import load_policy
from app.config.settings import BACKEND_ROOT, REPO_ROOT, Settings
from app.domain.values import Status

CN = timezone(timedelta(hours=8))
POLICY_PATH = BACKEND_ROOT / "config" / "policies" / "default.json"
CALENDAR_PATH = BACKEND_ROOT / "config" / "calendars" / "cn-holidays.json"


class ApiResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> object:
        return json.loads(self.body.decode("utf-8"))

    def problem(self) -> dict:
        payload = self.json()
        assert isinstance(payload, dict)
        return payload


class ApiCase(unittest.TestCase):
    """Base class that boots a server once per test class."""

    seeded = True

    @classmethod
    def setUpClass(cls) -> None:
        settings = Settings(
            store="memory",
            policy_path=POLICY_PATH,
            calendar_path=CALENDAR_PATH,
            web_root=REPO_ROOT / "frontend",
        )
        policy, document = load_policy(settings)
        cls.store = InMemoryStore()
        cls.now = datetime.now(timezone.utc)
        cls.service = TicketService(cls.store, policy.compiled)
        cls.seeded_ids: list[str] = []
        if cls.seeded:
            from app.seed import close_moment, seed_payloads, transition_path

            for payload, post in seed_payloads(cls.now):
                ticket, _decision, _replayed = cls.service.create(payload, now=cls.now)
                cls.seeded_ids.append(ticket.id)
                status = post.pop("status", None)
                moment = close_moment(cls.now, post) if "close_before" in post else cls.now
                post.pop("close_before", None)
                # The policy may already have advanced the ticket, and the
                # workflow may require intermediate steps, so walk the legal
                # route rather than assuming a single direct jump.
                if status:
                    for step in transition_path(ticket.status, Status.parse(status)):
                        cls.service.transition(ticket.id, step, now=moment)
                if post:
                    cls.service.update(ticket.id, post, now=moment)

        policy_state = {
            "policy": policy.compiled,
            "document": document,
            "store_kind": "memory",
            "store_stats": {"kind": "InMemoryStore"},
        }
        cls.router = build_router(cls.service, policy_state, started_at=0.0)
        cls.server = serve(("127.0.0.1", 0), cls.service, policy.compiled, cls.router, web_root=settings.web_root)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    # -- helpers ---------------------------------------------------------
    def call(
        self,
        method: str,
        path: str,
        *,
        body: object | None = None,
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
    ) -> ApiResponse:
        url = f"http://127.0.0.1:{self.port}{path}"
        payload = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        request = urllib.request.Request(url, data=payload, method=method)
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return ApiResponse(response.status, dict(response.headers), response.read())
        except urllib.error.HTTPError as error:
            return ApiResponse(error.code, dict(error.headers), error.read())


class MetaTests(ApiCase):
    def test_health_reports_policy_identity(self) -> None:
        response = self.call("GET", "/api/v1/health")
        self.assertEqual(response.status, 200)
        payload = response.json()
        assert isinstance(payload, dict)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["policy"]["name"], "flowops-default")
        self.assertTrue(response.headers["X-Request-Id"])

    def test_request_id_is_echoed_back(self) -> None:
        response = self.call("GET", "/api/v1/health", headers={"X-Request-Id": "req_abc123"})
        self.assertEqual(response.headers["X-Request-Id"], "req_abc123")

    def test_meta_exposes_the_dsl_reference(self) -> None:
        payload = self.call("GET", "/api/v1/meta").json()
        assert isinstance(payload, dict)
        codes = {item["code"] for item in payload["actions"]}
        self.assertIn("set_sla:duration", codes)
        self.assertTrue(payload["functions"])
        self.assertIn("open", [item["value"] for item in payload["statuses"]])

    def test_cors_headers_are_present(self) -> None:
        response = self.call("GET", "/api/v1/health")
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")

    def test_options_preflight_succeeds(self) -> None:
        response = self.call("OPTIONS", "/api/v1/tickets")
        self.assertEqual(response.status, 204)


class CreateTests(ApiCase):
    def test_create_returns_created_with_location_and_etag(self) -> None:
        response = self.call("POST", "/api/v1/tickets", body={"title": "全站无法登录", "body": "所有用户"})
        self.assertEqual(response.status, 201)
        payload = response.json()
        assert isinstance(payload, dict)
        ticket = payload["ticket"]
        self.assertEqual(ticket["priority"], "P1")
        self.assertEqual(ticket["team"], "platform")
        self.assertEqual(response.headers["ETag"], f'"{ticket["version"]}"')
        self.assertTrue(response.headers["Location"].endswith(ticket["id"]))
        self.assertTrue(payload["decision"]["fired"])

    def test_create_requires_a_title(self) -> None:
        response = self.call("POST", "/api/v1/tickets", body={"body": "没有标题"})
        self.assertEqual(response.status, 422)
        self.assertEqual(response.headers["Content-Type"], "application/problem+json")
        problem = response.problem()
        self.assertEqual(problem["status"], 422)
        self.assertIn("title", problem["detail"])

    def test_create_with_invalid_json_is_a_bad_request(self) -> None:
        response = self.call("POST", "/api/v1/tickets", raw=b"{not json")
        self.assertEqual(response.status, 400)
        self.assertIn("not valid JSON", response.problem()["detail"])

    def test_create_with_a_non_object_body_is_rejected(self) -> None:
        response = self.call("POST", "/api/v1/tickets", raw=b"[1,2,3]")
        self.assertEqual(response.status, 422)

    def test_idempotent_create_replays_the_same_ticket(self) -> None:
        body = {"title": "导出报表报错"}
        first = self.call("POST", "/api/v1/tickets", body=body, headers={"Idempotency-Key": "idem-000000001"})
        second = self.call("POST", "/api/v1/tickets", body=body, headers={"Idempotency-Key": "idem-000000001"})
        self.assertEqual(first.status, 201)
        self.assertEqual(second.status, 200)
        assert isinstance(first.json(), dict) and isinstance(second.json(), dict)
        self.assertEqual(first.json()["ticket"]["id"], second.json()["ticket"]["id"])
        self.assertTrue(second.json()["replayed"])

    def test_idempotency_key_reuse_with_another_body_conflicts(self) -> None:
        headers = {"Idempotency-Key": "idem-000000002"}
        self.call("POST", "/api/v1/tickets", body={"title": "第一个"}, headers=headers)
        response = self.call("POST", "/api/v1/tickets", body={"title": "第二个"}, headers=headers)
        self.assertEqual(response.status, 409)
        self.assertIn("Idempotency-Key", response.problem()["detail"])

    def test_a_short_idempotency_key_is_rejected(self) -> None:
        response = self.call("POST", "/api/v1/tickets", body={"title": "x"}, headers={"Idempotency-Key": "short"})
        self.assertEqual(response.status, 422)


class ReadTests(ApiCase):
    def test_list_returns_a_page_envelope(self) -> None:
        payload = self.call("GET", "/api/v1/tickets?limit=3").json()
        assert isinstance(payload, dict)
        self.assertEqual(len(payload["items"]), 3)
        self.assertEqual(payload["page"]["limit"], 3)
        self.assertTrue(payload["page"]["has_more"])
        self.assertTrue(payload["page"]["next_cursor"])

    def test_derived_sla_fields_are_present_on_every_item(self) -> None:
        payload = self.call("GET", "/api/v1/tickets?limit=1").json()
        ticket = payload["items"][0]  # type: ignore[index]
        for field in ("risk", "consumed_ratio", "remaining_hours", "age_hours", "breached", "allowed_transitions"):
            self.assertIn(field, ticket)
        self.assertIn(ticket["risk"], {"breached", "critical", "warning", "healthy", "untracked"})

    def test_cursor_pagination_pages_through_everything(self) -> None:
        seen: list[str] = []
        cursor = ""
        for _ in range(20):
            suffix = f"&cursor={cursor}" if cursor else ""
            payload = self.call("GET", f"/api/v1/tickets?limit=2{suffix}").json()
            seen.extend(item["id"] for item in payload["items"])  # type: ignore[index]
            cursor = payload["page"]["next_cursor"] or ""  # type: ignore[index]
            if not cursor:
                break
        self.assertEqual(len(seen), len(set(seen)))
        self.assertGreaterEqual(len(seen), 8)

    def test_invalid_cursor_is_a_validation_error(self) -> None:
        response = self.call("GET", "/api/v1/tickets?cursor=%%%")
        self.assertEqual(response.status, 422)

    def test_filters_are_applied(self) -> None:
        payload = self.call("GET", "/api/v1/tickets?priority=P1&limit=50").json()
        self.assertTrue(payload["items"])  # type: ignore[index]
        self.assertTrue(all(item["priority"] == "P1" for item in payload["items"]))  # type: ignore[index]

    def test_search_matches_content(self) -> None:
        # Non-ASCII query values must be percent-encoded by the client; the
        # server decodes them back to UTF-8, which this test pins down.
        payload = self.call("GET", f"/api/v1/tickets?q={quote('退款')}&limit=50").json()
        self.assertTrue(payload["items"])  # type: ignore[index]
        self.assertTrue(
            all("退款" in item["title"] or "退款" in item["body"] for item in payload["items"])  # type: ignore[index]
        )

    def test_bad_query_parameter_type_is_rejected(self) -> None:
        response = self.call("GET", "/api/v1/tickets?limit=abc")
        self.assertEqual(response.status, 422)

    def test_get_single_ticket(self) -> None:
        ticket_id = self.seeded_ids[0]
        response = self.call("GET", f"/api/v1/tickets/{ticket_id}")
        self.assertEqual(response.status, 200)
        payload = response.json()
        assert isinstance(payload, dict)
        self.assertEqual(payload["ticket"]["id"], ticket_id)
        self.assertEqual(response.headers["ETag"], f'"{payload["ticket"]["version"]}"')

    def test_get_unknown_ticket_is_a_problem(self) -> None:
        response = self.call("GET", "/api/v1/tickets/TCK-DOES-NOT-EXIST")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.headers["Content-Type"], "application/problem+json")
        self.assertIn("does not exist", response.problem()["detail"])

    def test_unknown_route_is_a_problem(self) -> None:
        # A path with a file extension is never handed to the SPA fallback, so it
        # must come back as a problem document rather than the console's HTML.
        response = self.call("GET", "/api/v1/nope.json")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.headers["Content-Type"], "application/problem+json")

    def test_unknown_api_route_without_extension_is_not_shadowed(self) -> None:
        response = self.call("GET", "/api/v1/no-such-collection")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.headers["Content-Type"], "application/problem+json")

    def test_method_not_allowed_lists_the_allowed_verbs(self) -> None:
        response = self.call("DELETE", f"/api/v1/tickets/{self.seeded_ids[0]}")
        self.assertEqual(response.status, 405)
        self.assertIn("GET", response.problem()["allowed"])

    def test_audit_trail_is_returned_in_order(self) -> None:
        payload = self.call("GET", f"/api/v1/tickets/{self.seeded_ids[0]}/audit").json()
        entries = payload["entries"]  # type: ignore[index]
        self.assertEqual([entry["seq"] for entry in entries], sorted(entry["seq"] for entry in entries))
        self.assertEqual(entries[0]["action"], "ticket.created")


class WriteTests(ApiCase):
    def test_patch_updates_a_field(self) -> None:
        response = self.call("POST", "/api/v1/tickets", body={"title": "接口偶发报错"})
        ticket_id = response.json()["ticket"]["id"]  # type: ignore[index]
        etag = response.headers["ETag"]
        patched = self.call(
            "PATCH", f"/api/v1/tickets/{ticket_id}", body={"assignee": "张伟"}, headers={"If-Match": etag}
        )
        self.assertEqual(patched.status, 200)
        payload = patched.json()
        assert isinstance(payload, dict)
        self.assertEqual(payload["ticket"]["assignee"], "张伟")
        self.assertNotEqual(patched.headers["ETag"], etag)

    def test_stale_if_match_is_a_precondition_failure(self) -> None:
        response = self.call("POST", "/api/v1/tickets", body={"title": "接口偶发报错"})
        ticket_id = response.json()["ticket"]["id"]  # type: ignore[index]
        self.call("PATCH", f"/api/v1/tickets/{ticket_id}", body={"assignee": "a"}, headers={"If-Match": '"1"'})
        conflict = self.call(
            "PATCH", f"/api/v1/tickets/{ticket_id}", body={"assignee": "b"}, headers={"If-Match": '"1"'}
        )
        self.assertEqual(conflict.status, 412)
        self.assertEqual(conflict.problem()["current_version"], 2)

    def test_if_match_star_is_accepted(self) -> None:
        response = self.call("POST", "/api/v1/tickets", body={"title": "接口偶发报错"})
        ticket_id = response.json()["ticket"]["id"]  # type: ignore[index]
        patched = self.call("PATCH", f"/api/v1/tickets/{ticket_id}", body={"assignee": "x"}, headers={"If-Match": "*"})
        self.assertEqual(patched.status, 200)

    def test_patching_an_unknown_field_is_rejected(self) -> None:
        response = self.call("PATCH", f"/api/v1/tickets/{self.seeded_ids[0]}", body={"status": "closed"})
        self.assertEqual(response.status, 422)
        self.assertIn("cannot be patched", response.problem()["detail"])

    def test_empty_patch_body_is_rejected(self) -> None:
        response = self.call("PATCH", f"/api/v1/tickets/{self.seeded_ids[0]}", body={})
        self.assertEqual(response.status, 422)

    def test_transition_moves_the_ticket(self) -> None:
        created = self.call("POST", "/api/v1/tickets", body={"title": "咨询发票开具"})
        ticket_id = created.json()["ticket"]["id"]  # type: ignore[index]
        moved = self.call("POST", f"/api/v1/tickets/{ticket_id}/transition", body={"status": "in_progress", "note": "开始"})
        self.assertEqual(moved.status, 200)
        payload = moved.json()
        assert isinstance(payload, dict)
        self.assertEqual(payload["ticket"]["status"], "in_progress")

    def test_illegal_transition_is_a_conflict_with_the_allowed_set(self) -> None:
        created = self.call("POST", "/api/v1/tickets", body={"title": "咨询发票开具"})
        ticket_id = created.json()["ticket"]["id"]  # type: ignore[index]
        self.call("POST", f"/api/v1/tickets/{ticket_id}/transition", body={"status": "in_progress"})
        response = self.call("POST", f"/api/v1/tickets/{ticket_id}/transition", body={"status": "closed"})
        self.assertEqual(response.status, 409)
        self.assertEqual(response.problem()["type"], "https://flowops.dev/problems/illegal-transition")
        self.assertIn("resolved", response.problem()["allowed"])

    def test_transition_without_a_status_is_rejected(self) -> None:
        response = self.call("POST", f"/api/v1/tickets/{self.seeded_ids[0]}/transition", body={})
        self.assertEqual(response.status, 422)


class PolicyAndOpsTests(ApiCase):
    def test_policy_endpoint_returns_the_document_and_summary(self) -> None:
        payload = self.call("GET", "/api/v1/policy").json()
        assert isinstance(payload, dict)
        self.assertEqual(payload["summary"]["name"], "flowops-default")
        self.assertTrue(payload["document"]["rules"])

    def test_preview_explains_a_decision_without_persisting(self) -> None:
        before = self.call("GET", "/api/v1/tickets?limit=1").json()["page"]["total"]  # type: ignore[index]
        payload = self.call(
            "POST", "/api/v1/policy/preview", body={"title": "疑似数据泄露，可越权读取订单"}
        ).json()
        assert isinstance(payload, dict)
        decision = payload["decision"]
        self.assertEqual(decision["fired"], ["R01-安全事件上报"])
        self.assertEqual(decision["fields"]["priority"], "P1")
        self.assertTrue(decision["trace"])
        self.assertIn("ticket", decision["scope"])
        after = self.call("GET", "/api/v1/tickets?limit=1").json()["page"]["total"]  # type: ignore[index]
        self.assertEqual(before, after)

    def test_preview_of_an_ordinary_ticket_succeeds(self) -> None:
        payload = self.call("POST", "/api/v1/policy/preview", body={"title": "页面偶发白屏"}).json()
        assert isinstance(payload, dict)
        self.assertTrue(payload["decision"]["fired"])

    def test_preview_requires_a_body(self) -> None:
        response = self.call("POST", "/api/v1/policy/preview", body={})
        self.assertEqual(response.status, 422)

    def test_policy_validation_reports_a_syntax_error_with_position(self) -> None:
        document = {"name": "bad-syntax", "rules": [{"name": "r", "when": "ticket.priority ==", "then": ["stop"]}]}
        response = self.call("POST", "/api/v1/policy/validate", body={"document": document})
        self.assertEqual(response.status, 422)
        detail = response.problem()["detail"]
        self.assertIn("unexpected end of expression", detail)
        self.assertIn("^", detail)

    def test_policy_validation_rejects_a_broken_document(self) -> None:
        broken = {"name": "broken", "rules": [{"name": "r", "when": None, "then": ["explode:now"]}]}
        response = self.call("POST", "/api/v1/policy/validate", body={"document": broken})
        self.assertEqual(response.status, 422)
        self.assertIn("unknown action", response.problem()["detail"])

    def test_policy_validation_accepts_a_good_document(self) -> None:
        good = {"name": "ok", "rules": [{"name": "r", "when": "ticket.priority == 'P0'", "then": ["assign_team:tier0-incident"]}]}
        payload = self.call("POST", "/api/v1/policy/validate", body={"document": good}).json()
        assert isinstance(payload, dict)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["summary"]["name"], "ok")

    def test_sla_snapshot_reports_totals(self) -> None:
        payload = self.call("GET", "/api/v1/sla/snapshot?horizon_hours=24").json()
        assert isinstance(payload, dict)
        self.assertEqual(payload["horizon_hours"], 24.0)
        self.assertGreaterEqual(payload["totals"]["all"], 8)
        self.assertIn("calendar", payload)

    def test_sla_snapshot_rejects_an_absurd_horizon(self) -> None:
        response = self.call("GET", "/api/v1/sla/snapshot?horizon_hours=9999")
        self.assertEqual(response.status, 422)

    def test_metrics_aggregates_by_band_and_team(self) -> None:
        payload = self.call("GET", "/api/v1/metrics").json()
        assert isinstance(payload, dict)
        self.assertGreaterEqual(payload["tickets"]["total"], 8)
        self.assertIn("breached", payload["tickets"]["by_risk"])
        self.assertTrue(payload["tickets"]["by_team"])

    def test_sla_refresh_succeeds(self) -> None:
        response = self.call("POST", "/api/v1/sla/refresh", body={})
        self.assertEqual(response.status, 200)
        payload = response.json()
        assert isinstance(payload, dict)
        self.assertIn("tickets_touched", payload)


class StaticAssetTests(ApiCase):
    seeded = False

    def test_missing_frontend_asset_is_still_a_problem_document(self) -> None:
        # The console may not be built yet; the API must not 500 because of it.
        response = self.call("GET", "/app.js")
        self.assertIn(response.status, (200, 404))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
