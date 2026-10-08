"""End-to-end smoke test against a **running** FlowOps server.

Unlike the unit and integration suites, this talks to a real process over a real
socket, which is the only way to catch the class of bug that lives in the gaps:
a missing static asset, a Content-Type the browser refuses, a route the SPA
fallback swallows.

Usage::

    python backend/scripts/smoke.py --base http://127.0.0.1:8787

Exit code 0 means every check passed.  Any failure prints what was expected and
what came back, and exits 1 — so it is safe to wire into CI or a pre-demo check.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

PASS = "PASS"
FAIL = "FAIL"

ASSETS = (
    "/",
    "/index.html",
    "/src/app.js",
    "/src/store.js",
    "/src/styles/tokens.css",
    "/src/styles/app.css",
    "/src/lib/risk.js",
    "/src/lib/catalog.js",
    "/src/lib/python-bridge.js",
    "/src/views/sandbox.js",
    "/data/snapshot.json",
    "/data/flowops-engine.zip",
)


class Checker:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.failures: list[str] = []
        self.checks = 0

    def call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(f"{self.base}{path}", data=payload, method=method)
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def expect(self, label: str, condition: bool, detail: str = "") -> bool:
        self.checks += 1
        if condition:
            print(f"  {PASS}  {label}")
            return True
        self.failures.append(label)
        print(f"  {FAIL}  {label}{(' — ' + detail) if detail else ''}")
        return False

    def json_of(self, payload: bytes) -> Any:
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FlowOps end-to-end smoke test")
    parser.add_argument("--base", default="http://127.0.0.1:8787", help="server base URL")
    parser.add_argument("--skip-writes", action="store_true", help="read-only checks only")
    args = parser.parse_args(argv)

    checker = Checker(args.base)
    print(f"FlowOps smoke test against {checker.base}\n")

    print("meta")
    status, _headers, body = checker.call("GET", "/api/v1/health")
    health = checker.json_of(body) or {}
    checker.expect("GET /api/v1/health -> 200", status == 200, f"got {status}")
    checker.expect("health reports a policy", bool(health.get("policy", {}).get("hash")))
    checker.expect("health reports a store", bool(health.get("store")))

    status, _headers, body = checker.call("GET", "/api/v1/meta")
    meta = checker.json_of(body) or {}
    checker.expect("GET /api/v1/meta -> 200", status == 200, f"got {status}")
    checker.expect("meta exposes the action catalog", bool(meta.get("actions")))
    checker.expect("meta exposes the function library", bool(meta.get("functions")))
    checker.expect("meta exposes the compiled rules", bool(meta.get("policy", {}).get("rules")))

    print("\nsla + tickets")
    status, _headers, body = checker.call("GET", "/api/v1/sla/snapshot?horizon_hours=24")
    snapshot = checker.json_of(body) or {}
    checker.expect("GET /api/v1/sla/snapshot -> 200", status == 200, f"got {status}")
    checker.expect("snapshot reports totals", "all" in (snapshot.get("totals") or {}))
    checker.expect("snapshot describes the calendar", bool(snapshot.get("calendar")))

    status, headers, body = checker.call("GET", "/api/v1/tickets?limit=5")
    page = checker.json_of(body) or {}
    checker.expect("GET /api/v1/tickets -> 200", status == 200, f"got {status}")
    checker.expect("ticket page carries a cursor envelope", "page" in page)
    items = page.get("items") or []
    checker.expect("listed tickets expose derived SLA fields", bool(items) and "risk" in items[0])
    checker.expect("list response sets X-Total-Count", "X-Total-Count" in headers)

    status, _headers, body = checker.call("GET", "/api/v1/tickets/TCK-DOES-NOT-EXIST")
    problem = checker.json_of(body) or {}
    checker.expect("unknown ticket -> 404", status == 404, f"got {status}")
    checker.expect("404 body is a problem document", problem.get("type", "").startswith("https://"))

    status, _headers, _body = checker.call("GET", "/api/v1/not-a-route")
    checker.expect("unknown API route -> 404 (not the SPA)", status == 404, f"got {status}")

    print("\npolicy sandbox")
    status, _headers, body = checker.call(
        "POST", "/api/v1/policy/preview", body={"title": "疑似数据泄露，可越权读取订单"}
    )
    preview = checker.json_of(body) or {}
    decision = preview.get("decision") or {}
    checker.expect("POST /api/v1/policy/preview -> 200", status == 200, f"got {status}")
    checker.expect("security ticket hits the security rule", decision.get("fired") == ["R01-安全事件上报"], str(decision.get("fired")))
    checker.expect("preview explains itself with a trace", bool(decision.get("trace")))
    checker.expect("preview resolves an SLA deadline", bool((decision.get("fields") or {}).get("sla_due_at")))

    print("\nstatic console")
    for asset in ASSETS:
        status, headers, body = checker.call("GET", asset)
        content_type = headers.get("Content-Type", "")
        checker.expect(f"GET {asset} -> 200", status == 200, f"got {status}")
        if status == 200:
            if asset.endswith(".js"):
                checker.expect(f"{asset} served as JavaScript", "javascript" in content_type, content_type)
            elif asset.endswith(".css"):
                checker.expect(f"{asset} served as CSS", "css" in content_type, content_type)
            elif asset.endswith(".json"):
                checker.expect(f"{asset} parses as JSON", checker.json_of(body) is not None)
            elif asset.endswith(".zip"):
                checker.expect(f"{asset} looks like a zip", body[:2] == b"PK")

    if not args.skip_writes:
        print("\nwrites (idempotency + state machine)")
        payload = {"title": "冒烟验证：导出报表报错"}
        key = "smoke-00000001"
        status, headers, body = checker.call(
            "POST", "/api/v1/tickets", body=payload, headers={"Idempotency-Key": key}
        )
        created = checker.json_of(body) or {}
        ticket = created.get("ticket") or {}
        checker.expect("POST /api/v1/tickets -> 201", status == 201, f"got {status}")
        checker.expect("create returns an ETag", "ETag" in headers)
        checker.expect("create triages the ticket", bool(ticket.get("team")))
        checker.expect("create explains its decision", bool((created.get("decision") or {}).get("fired")))

        status, _headers, body = checker.call(
            "POST", "/api/v1/tickets", body=payload, headers={"Idempotency-Key": key}
        )
        replay = checker.json_of(body) or {}
        checker.expect("replaying the key -> 200", status == 200, f"got {status}")
        checker.expect("replay returns the same ticket", (replay.get("ticket") or {}).get("id") == ticket.get("id"))
        checker.expect("replay is flagged", replay.get("replayed") is True)

        ticket_id = ticket.get("id")
        version = ticket.get("version", 1)

        # A triaged ticket cannot jump straight to closed: resolution is required
        # first, and the API must say so with a typed conflict.
        status, _headers, body = checker.call(
            "POST", f"/api/v1/tickets/{ticket_id}/transition", body={"status": "closed"}
        )
        conflict = checker.json_of(body) or {}
        checker.expect("illegal transition -> 409", status == 409, f"got {status}")
        checker.expect(
            "conflict names the transitions that are legal from here",
            "in_progress" in (conflict.get("allowed") or [])
            and "closed" not in (conflict.get("allowed") or []),
            str(conflict.get("allowed")),
        )
        checker.expect(
            "conflict is a typed problem document",
            conflict.get("type", "").endswith("illegal-transition"),
            str(conflict.get("type")),
        )

        # Optimistic concurrency: the ETag from the create response is still
        # valid because nothing has written to the ticket yet.
        status, _headers, body = checker.call(
            "PATCH",
            f"/api/v1/tickets/{ticket_id}",
            body={"assignee": "smoke-bot"},
            headers={"If-Match": f'"{version}"'},
        )
        patched = checker.json_of(body) or {}
        checker.expect("PATCH with a fresh ETag -> 200", status == 200, f"got {status}")
        checker.expect("assignee was stored", (patched.get("ticket") or {}).get("assignee") == "smoke-bot")
        checker.expect(
            "a successful write advances the ETag",
            patched.get("ticket", {}).get("version") == version + 1,
            f"version {(patched.get('ticket') or {}).get('version')}",
        )

        status, _headers, _body = checker.call(
            "PATCH",
            f"/api/v1/tickets/{ticket_id}",
            body={"assignee": "stale-writer"},
            headers={"If-Match": f'"{version}"'},
        )
        checker.expect("stale If-Match -> 412", status == 412, f"got {status}")

        status, _headers, body = checker.call("GET", f"/api/v1/tickets/{ticket_id}/audit")
        trail = checker.json_of(body) or {}
        entries = trail.get("entries") or []
        checker.expect("audit trail is recorded", len(entries) >= 2, f"{len(entries)} entries")
        checker.expect(
            "audit entries are ordered",
            [entry.get("seq") for entry in entries] == sorted(entry.get("seq") for entry in entries),
        )

    print()
    total = checker.checks
    if checker.failures:
        print(f"{FAIL}: {len(checker.failures)}/{total} checks failed")
        for item in checker.failures:
            print(f"  - {item}")
        return 1
    print(f"{PASS}: all {total} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
