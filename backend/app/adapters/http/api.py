"""The HTTP API surface.

Design rules that the endpoints below actually follow:

* **Reads are cheap and time-aware.** Every ticket response carries derived SLA
  fields (``risk``, ``remaining_hours``, ``breached``) computed at request time,
  so the console never has to re-implement the calendar.
* **Writes are idempotent when asked.** ``Idempotency-Key`` replays the original
  response instead of creating a duplicate; reusing a key with a different body
  is a 409, not a silent overwrite.
* **Writes are concurrency-safe when asked.** ``If-Match``/``ETag`` turn a lost
  update into a 412 instead of a data-loss bug.
* **Errors are typed.** Every failure is RFC 9457 ``application/problem+json``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Mapping

from ...application.models import TicketQuery
from ...application.services.tickets import KNOWN_TEAMS, PATCHABLE, TicketService
from ...domain.errors import ValidationError
from ...domain.rules.engine import describe_actions, describe_functions, policy_summary
from ...domain.values import RiskBand, Status
from .problem import Problem
from .server import Request, Response, Router

API_VERSION = "1.0.0"


def build_router(service: TicketService, policy_state: dict[str, Any], *, started_at: float) -> Router:
    router = Router()

    # -- meta ------------------------------------------------------------
    def health(_request: Request) -> Response:
        return Response(
            body={
                "status": "ok",
                "version": API_VERSION,
                "uptime_seconds": round(perf_counter() - started_at, 3),
                "policy": {
                    "name": policy_state["policy"].name,
                    "version": policy_state["policy"].version,
                    "hash": policy_state["policy"].source_hash,
                },
                "store": policy_state.get("store_kind", "unknown"),
            }
        )

    def meta(_request: Request) -> Response:
        compiled = policy_state["policy"]
        return Response(
            body={
                "service": "flowops",
                "version": API_VERSION,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "statuses": [{"value": item.value, "label": item.label} for item in Status],
                "risk_bands": [item.value for item in RiskBand],
                "teams": sorted(KNOWN_TEAMS),
                "patchable_fields": sorted(PATCHABLE),
                "actions": describe_actions(),
                "functions": describe_functions(),
                "policy": policy_summary(compiled),
                "calendar": compiled.calendar.describe(),
            }
        )

    # -- tickets ---------------------------------------------------------
    def list_tickets(request: Request) -> Response:
        query = TicketQuery(
            status=request.q_many("status") or None,
            priority=request.q_many("priority") or None,
            team=request.q("team"),
            risk=request.q_many("risk") or None,
            search=request.q("q"),
            sort=request.q("sort", "-created_at") or "-created_at",
            limit=request.q_int("limit", 50),
            cursor=request.q("cursor"),
        )
        page = service.list(query)
        payload = {
            "items": [ticket.summary() for ticket in page.items],
            "page": {
                "total": page.total,
                "count": len(page.items),
                "has_more": page.has_more,
                "next_cursor": page.next_cursor,
                "limit": query.limit,
                "sort": query.sort,
            },
        }
        return Response(body=payload, headers={"X-Total-Count": str(page.total)})

    def create_ticket(request: Request) -> Response:
        idempotency_key = request.header("idempotency-key")
        if idempotency_key is not None and not 8 <= len(idempotency_key) <= 128:
            raise ValidationError("Idempotency-Key must be 8..128 characters")
        ticket, decision, replayed = service.create(
            request.body,
            actor=request.header("x-actor", "api") or "api",
            idempotency_key=idempotency_key,
            request_fingerprint=None,
        )
        payload = {
            "ticket": ticket.summary(),
            "decision": decision.as_dict() if decision else None,
            "replayed": replayed,
        }
        status = 200 if replayed else 201
        headers = {"ETag": f'"{ticket.version}"', "Location": f"/api/v1/tickets/{ticket.id}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return Response(status=status, body=payload, headers=headers)

    def get_ticket(request: Request) -> Response:
        ticket = service.get(request.params["ticket_id"])
        return Response(body={"ticket": ticket.summary()}, headers={"ETag": f'"{ticket.version}"'})

    def patch_ticket(request: Request) -> Response:
        if not request.body:
            raise ValidationError("request body must contain at least one field to patch")
        expected = _expected_version(request)
        ticket = service.update(
            request.params["ticket_id"],
            request.body,
            actor=request.header("x-actor", "api") or "api",
            expected_version=expected,
        )
        return Response(body={"ticket": ticket.summary()}, headers={"ETag": f'"{ticket.version}"'})

    def transition_ticket(request: Request) -> Response:
        target = request.body.get("status") or request.body.get("to")
        if not target:
            raise ValidationError("body must contain 'status'")
        expected = _expected_version(request)
        ticket = service.transition(
            request.params["ticket_id"],
            str(target),
            actor=request.header("x-actor", "api") or "api",
            note=request.body.get("note"),
            expected_version=expected,
        )
        return Response(body={"ticket": ticket.summary()}, headers={"ETag": f'"{ticket.version}"'})

    def ticket_audit(request: Request) -> Response:
        ticket_id = request.params["ticket_id"]
        entries = service.trail(ticket_id, limit=request.q_int("limit", 200))
        return Response(body={"ticket_id": ticket_id, "entries": [entry.as_dict() for entry in entries]})

    def preview(request: Request) -> Response:
        if not request.body:
            raise ValidationError("body must contain the ticket fields to evaluate")
        if not isinstance(request.body, Mapping):
            raise ValidationError("body must be a JSON object of ticket fields")
        decision = service.preview(request.body)
        return Response(body={"decision": decision.as_dict(include_scope=True)})

    # -- operations ------------------------------------------------------
    def sla_snapshot(request: Request) -> Response:
        horizon = request.q_float("horizon_hours", 4.0)
        if not 0 < horizon <= 24 * 30:
            raise ValidationError("horizon_hours must be between 0 and 720")
        return Response(body=service.sla_snapshot(horizon_hours=horizon))

    def refresh_sla(request: Request) -> Response:
        return Response(body=service.refresh_sla())

    def metrics(_request: Request) -> Response:
        tickets = service.store.all_tickets()
        by_status: dict[str, int] = {status.value: 0 for status in Status}
        by_priority: dict[str, int] = {}
        by_team: dict[str, int] = {}
        by_risk: dict[str, int] = {band.value: 0 for band in RiskBand}
        breached = 0
        first_response: list[float] = []
        for ticket in tickets:
            by_status[ticket.status.value] = by_status.get(ticket.status.value, 0) + 1
            by_priority[ticket.priority.value] = by_priority.get(ticket.priority.value, 0) + 1
            by_team[ticket.team or "unassigned"] = by_team.get(ticket.team or "unassigned", 0) + 1
            by_risk[ticket.risk().value] = by_risk.get(ticket.risk().value, 0) + 1
            if ticket.is_breached():
                breached += 1
            if ticket.status not in (Status.OPEN,):
                first_response.append(ticket.age_hours())
        closed = [ticket for ticket in tickets if ticket.closed_at]
        met = sum(
            1
            for ticket in closed
            if ticket.sla_due_at is not None and ticket.closed_at is not None and ticket.closed_at <= ticket.sla_due_at
        )
        return Response(
            body={
                "tickets": {
                    "total": len(tickets),
                    "by_status": by_status,
                    "by_priority": by_priority,
                    "by_team": by_team,
                    "by_risk": by_risk,
                    "breached": breached,
                },
                "sla": {
                    "closed_with_sla": len([t for t in closed if t.sla_due_at]),
                    "closed_met_target": met,
                    "attainment_ratio": round(met / len(closed), 4) if closed else None,
                    "median_triage_hours": round(sorted(first_response)[len(first_response) // 2], 3)
                    if first_response
                    else None,
                },
                "store": policy_state.get("store_stats", {}),
            }
        )

    def policy_document(_request: Request) -> Response:
        return Response(
            body={
                "document": policy_state["document"],
                "summary": policy_summary(policy_state["policy"]),
            }
        )

    def validate_policy(request: Request) -> Response:
        """Compile a candidate policy without installing it (used by the editor)."""
        from ...domain.rules.engine import Policy as PolicyModel

        document = request.body.get("document") if "document" in request.body else request.body
        if not isinstance(document, Mapping):
            raise ValidationError("body must contain a policy object or {'document': {...}}")
        candidate = PolicyModel.from_dict(document)
        return Response(body={"valid": True, "summary": policy_summary(candidate.compiled)})

    router.add("GET", r"/api/v1/health", health, "health")
    router.add("GET", r"/api/v1/meta", meta, "meta")
    router.add("GET", r"/api/v1/tickets", list_tickets, "tickets.list")
    router.add("POST", r"/api/v1/tickets", create_ticket, "tickets.create")
    router.add("GET", r"/api/v1/tickets/(?P<ticket_id>[A-Za-z0-9\-_.]+)", get_ticket, "tickets.get")
    router.add("PATCH", r"/api/v1/tickets/(?P<ticket_id>[A-Za-z0-9\-_.]+)", patch_ticket, "tickets.patch")
    router.add(
        "POST",
        r"/api/v1/tickets/(?P<ticket_id>[A-Za-z0-9\-_.]+)/transition",
        transition_ticket,
        "tickets.transition",
    )
    router.add(
        "GET",
        r"/api/v1/tickets/(?P<ticket_id>[A-Za-z0-9\-_.]+)/audit",
        ticket_audit,
        "tickets.audit",
    )
    router.add("POST", r"/api/v1/policy/preview", preview, "policy.preview")
    router.add("GET", r"/api/v1/policy", policy_document, "policy.get")
    router.add("POST", r"/api/v1/policy/validate", validate_policy, "policy.validate")
    router.add("GET", r"/api/v1/sla/snapshot", sla_snapshot, "sla.snapshot")
    router.add("POST", r"/api/v1/sla/refresh", refresh_sla, "sla.refresh")
    router.add("GET", r"/api/v1/metrics", metrics, "metrics")
    router.add("GET", r"/readyz", health, "readyz")
    router.add("GET", r"/healthz", health, "healthz")
    return router


def _expected_version(request: Request) -> int | None:
    raw = request.header("if-match")
    if raw is None or raw.strip() in ("", "*"):
        return None
    token = raw.strip()
    if token.startswith("W/"):  # weak validator
        token = token[2:]
    token = token.strip('"')
    try:
        return int(token)
    except ValueError as exc:
        raise ValidationError(f"If-Match must be a version number or *, got {raw!r}") from exc


def error_payload(problem: Problem) -> bytes:
    return problem.to_json()


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)
