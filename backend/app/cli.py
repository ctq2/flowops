"""FlowOps command line: serve the API, seed data, or inspect the calendar."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

if __package__ in (None, ""):  # allow `python app/cli.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import __version__
from app.adapters.http.api import build_router
from app.adapters.http.server import serve
from app.adapters.persistence.memory import InMemoryStore
from app.adapters.persistence.sqlite_store import SqliteStore
from app.application.services.tickets import ListEventSink, TicketService
from app.bootstrap import (
    build_service,
    build_store,
    holiday_summary,
    load_policy,
    upcoming_holidays,
)
from app.config.settings import Settings
from app.domain.errors import DomainError
from app.domain.rules.engine import describe_actions, describe_functions, policy_summary
from app.domain.rules.evaluator import to_jsonable
from app.domain.values import RiskBand, Status
from app.policy_lint import format_report, lint_policy
from app.seed import close_moment, seed_payloads, transition_path

CN = timezone(timedelta(hours=8))


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stdout,
    )


def cmd_serve(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    if args.seed:
        _seed(settings, now=datetime.now(timezone.utc))
    service, policy, document, store = build_service(settings)
    started = perf_counter()
    policy_state: dict[str, object] = {
        "policy": policy,
        "document": document,
        "store_kind": settings.store,
        "store_stats": _store_stats(store),
    }
    router = build_router(service, policy_state, started_at=started)
    web_root = None if args.no_web else settings.web_root
    server = serve(
        (settings.host, settings.port),
        service,
        policy,
        router,
        web_root=web_root,
        cors_origin=settings.cors_origin,
    )
    print(f"FlowOps {__version__} listening on http://{settings.host}:{settings.port}")
    print(f"  console : http://{settings.host}:{settings.port}/" if web_root else "  console : (static UI disabled)")
    print(f"  store   : {settings.store} ({settings.database if settings.store == 'sqlite' else 'in-memory'})")
    print(f"  policy  : {policy.name} v{policy.version} ({policy.source_hash})")
    print(f"  calendar: {policy.calendar.describe()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    result = _seed(settings, now=datetime.now(timezone.utc), reset=args.reset)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_calendar(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    summary = holiday_summary(settings)
    summary["next_closed_and_make_up_days"] = upcoming_holidays(
        settings, after=datetime.now(CN).date(), limit=args.limit
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    settings = _settings_from(args)
    policy, document = load_policy(settings)
    if args.show_document:
        print(json.dumps(document, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(policy_summary(policy.compiled), ensure_ascii=False, indent=2))
        print("\n# available actions")
        for item in describe_actions():
            print(f"  {item['code']:<20} {item['docs']}")
        print("\n# available functions")
        for item in describe_functions():
            print(f"  {item['signature']:<38} {item['docs']}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Validate configuration without starting anything — handy in CI."""
    settings = _settings_from(args)
    policy, document = load_policy(settings)
    memory_settings = Settings(store="memory", policy_path=settings.policy_path, calendar_path=settings.calendar_path)
    store = build_store(memory_settings)
    service = TicketService(store, policy.compiled)
    report = {
        "policy": {"name": policy.name, "version": policy.version, "hash": policy.source_hash},
        "rules": len(policy.compiled.rules),
        "fallback_rules": len(policy.compiled.fallback),
        "calendar": policy.compiled.calendar.describe(),
        "functions": len(describe_functions()),
        "actions": len(describe_actions()),
        "preview_fired": [],
    }
    now = datetime.now(timezone.utc)
    for payload, _post in seed_payloads(now):
        decision = service.preview(payload, now=now)
        report["preview_fired"].append({"title": payload["title"][:40], "rules": decision.fired})
    report["statuses"] = [status.value for status in Status]
    report["risk_bands"] = [band.value for band in RiskBand]

    # `--lint` turns the smoke check into a gate: every rule must be reachable.
    lint = lint_policy(policy.compiled, document=document)
    report["lint"] = lint.as_dict()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("\n" + format_report(lint), file=sys.stderr)
    if not lint.ok:
        print("error: policy lint found errors", file=sys.stderr)
        return 1
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Print a full policy walk-through — the thing you paste into a README."""
    settings = _settings_from(args)
    policy, _document = load_policy(settings)
    service = TicketService(InMemoryStore(), policy.compiled)
    now = datetime.now(timezone.utc)
    lines: list[str] = []
    for payload, _post in seed_payloads(now):
        decision = service.preview(payload, now=now)
        lines.append(f"\n### {payload['title']}")
        lines.append(f"  规则命中 : {', '.join(decision.fired) or '(fallback)'}")
        lines.append(f"  定级     : {decision.fields.get('priority')}")
        fields = decision.fields
        lines.append(f"  时限     : {fields.get('sla_duration_hours')} 小时（{fields.get('sla_calendar')}）")
        lines.append(f"  已消耗   : {fields.get('sla_consumed_hours')} 小时")
        lines.append(f"  剩余     : {fields.get('sla_remaining_hours')} 小时")
        due = fields.get("sla_due_at")
        lines.append(f"  到期时间 : {due.isoformat() if due else '-'}")
        if decision.notifications:
            channels = ", ".join(f"{item.channel}(+{item.after_hours}h)" for item in decision.notifications)
            lines.append(f"  升级通知 : {channels}")
        for entry in decision.trace:
            marker = "ok " if entry.ok else "ERR"
            lines.append(f"    [{marker}] {entry.rule} → {entry.action} {entry.before!r} => {entry.after!r}")
    print("\n".join(lines))
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    """Static analysis for the policy document — run this in review."""
    settings = _settings_from(args)
    policy, document = load_policy(settings)
    report = lint_policy(policy.compiled, document=document)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
        if args.verbose:
            print("\n# per-rule detail")
            for item in policy.compiled.rules:
                print(f"  {item.rule.priority:>4}  {item.rule.name:<28} {item.rule.when or '(always)'}")
    return 0 if report.ok else 1


def cmd_export(args: argparse.Namespace) -> int:
    """Write a static JSON snapshot of the demo data for the offline UI.

    The snapshot is what makes the console work from ``file://`` with no server
    and no network: every number the UI shows is produced by the real Python
    engine, then frozen.  Opened over HTTP the same UI prefers the live API.
    """
    settings = _settings_from(args)
    now = datetime.now(timezone.utc)
    policy, document = load_policy(settings)
    store = InMemoryStore()
    service = TicketService(store, policy.compiled, events=ListEventSink())
    for payload, post in seed_payloads(now):
        ticket, _decision, _replayed = service.create(payload, now=now)
        status = post.pop("status", None)
        moment = close_moment(now, post) if "close_before" in post else now
        post.pop("close_before", None)
        if status:
            for step in transition_path(ticket.status, Status.parse(status)):
                service.transition(ticket.id, step, now=moment)
        if post:
            service.update(ticket.id, post, now=moment)

    tickets = store.all_tickets()
    snapshot = {
        "generated_at": now.isoformat(),
        "policy": to_jsonable(policy_summary(policy.compiled)),
        # The raw document travels too: the in-browser sandbox runs the real
        # engine, and it needs the policy as authored, not a summary of it.
        "document": document,
        "tickets": [ticket.summary(now) for ticket in tickets],
        "sla": service.sla_snapshot(now=now, horizon_hours=8),
        "histograms": {
            "risk": service.risk_histogram(now=now),
            "priority": _count_by(tickets, lambda item: item.priority.value),
            "team": _count_by(tickets, lambda item: item.team or "unassigned"),
            "status": _count_by(tickets, lambda item: item.status.value),
        },
        "upcoming": upcoming_holidays(settings, after=now.astimezone(CN).date(), limit=8),
    }
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"wrote {target} ({target.stat().st_size} bytes, {len(snapshot['tickets'])} tickets)")
    return 0


def _count_by(tickets: list, key) -> dict[str, int]:  # type: ignore[type-arg]
    counts: dict[str, int] = {}
    for ticket in tickets:
        label = key(ticket)
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _settings_from(args: argparse.Namespace) -> Settings:
    settings = Settings()
    if getattr(args, "store", None):
        settings.store = args.store
    if getattr(args, "db", None):
        settings.database = Path(args.db)
    if getattr(args, "policy", None):
        settings.policy_path = Path(args.policy)
    if getattr(args, "web_root", None):
        settings.web_root = Path(args.web_root)
    if getattr(args, "host", None):
        settings.host = args.host
    if getattr(args, "port", None):
        settings.port = args.port
    _configure_logging(getattr(args, "log_level", "info") or "info")
    return settings


def _store_stats(store: object) -> dict[str, object]:
    stats: dict[str, object] = {"kind": type(store).__name__}
    counts = getattr(store, "table_counts", None)
    if callable(counts):
        stats["tables"] = counts()
    size = getattr(store, "size_bytes", None)
    if callable(size):
        stats["bytes"] = size()
    return stats


def _seed(settings: Settings, *, now: datetime, reset: bool = False) -> dict[str, object]:
    if settings.store == "sqlite":
        settings.database.parent.mkdir(parents=True, exist_ok=True)
        if reset and settings.database.exists():
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(f"{settings.database}{suffix}")
                if candidate.exists():
                    candidate.unlink()
    policy, _document = load_policy(settings)
    store = SqliteStore(settings.database) if settings.store == "sqlite" else InMemoryStore()
    service = TicketService(store, policy.compiled, events=ListEventSink())
    created = 0
    skipped = 0
    for payload, post in seed_payloads(now):
        if store.get(payload["id"]) is not None:
            skipped += 1
            continue
        ticket, _decision, _replayed = service.create(payload, actor="seed", now=now)
        created += 1
        status = post.pop("status", None)
        moment = close_moment(now, post) if "close_before" in post else now
        post.pop("close_before", None)
        if status:
            try:
                for step in transition_path(ticket.status, Status.parse(status)):
                    service.transition(ticket.id, step, actor="seed", now=moment)
            except DomainError as error:  # pragma: no cover - data drift guard
                logging.warning("seed: %s -> %s rejected: %s", ticket.id, status, error.detail)
        if post:
            service.update(ticket.id, post, actor="seed", now=moment)
    snapshot = service.sla_snapshot(now=now)
    closer = getattr(store, "close", None)
    if callable(closer):
        closer()
    return {
        "created": created,
        "skipped": skipped,
        "store": settings.store,
        "database": str(settings.database) if settings.store == "sqlite" else None,
        "totals": snapshot["totals"],
        "histogram": {band.value: count for band, count in zip(RiskBand, _risk_counts(store))},
    }


def _risk_counts(store: object) -> list[int]:
    histogram = getattr(store, "risk_histogram", None)
    if callable(histogram):
        values = histogram()
        return [values.get(band.value, 0) for band in RiskBand]
    return [0] * len(RiskBand)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flowops", description="FlowOps — SLA 工单运营平台")
    parser.add_argument("--version", action="version", version=f"flowops {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(target: argparse.ArgumentParser, *, with_server: bool = False) -> None:
        target.add_argument("--store", choices=("sqlite", "memory"), default="sqlite")
        target.add_argument("--db", default=None, help="SQLite file path")
        target.add_argument("--policy", default=None, help="policy JSON path")
        target.add_argument("--web-root", default=None, help="static console directory")
        target.add_argument("--log-level", default="info")
        if with_server:
            target.add_argument("--host", default="127.0.0.1")
            target.add_argument("--port", type=int, default=8787)
            target.add_argument("--no-web", action="store_true", help="serve only the JSON API")
            target.add_argument("--seed", action="store_true", help="load demo tickets before serving")

    serve_parser = sub.add_parser("serve", help="start the HTTP API and console")
    common(serve_parser, with_server=True)
    serve_parser.set_defaults(func=cmd_serve)

    seed_parser = sub.add_parser("seed", help="insert the demo dataset")
    common(seed_parser)
    seed_parser.add_argument("--reset", action="store_true", help="delete the SQLite file first")
    seed_parser.set_defaults(func=cmd_seed)

    calendar_parser = sub.add_parser("calendar", help="inspect the holiday calendar")
    common(calendar_parser)
    calendar_parser.add_argument("--limit", type=int, default=12)
    calendar_parser.set_defaults(func=cmd_calendar)

    policy_parser = sub.add_parser("policy", help="print the compiled policy and DSL reference")
    common(policy_parser)
    policy_parser.add_argument("--document", action="store_true", dest="show_document")
    policy_parser.set_defaults(func=cmd_policy)

    check_parser = sub.add_parser("check", help="validate configuration (CI friendly)")
    common(check_parser)
    check_parser.set_defaults(func=cmd_check)

    lint_parser = sub.add_parser("lint", help="static analysis for the policy document")
    common(lint_parser)
    lint_parser.add_argument("--json", action="store_true", help="emit machine-readable findings")
    lint_parser.add_argument("--verbose", action="store_true", help="list every rule in execution order")
    lint_parser.set_defaults(func=cmd_lint)

    demo_parser = sub.add_parser("demo", help="explain the policy decision for each demo ticket")
    common(demo_parser)
    demo_parser.set_defaults(func=cmd_demo)

    export_parser = sub.add_parser("export", help="write a static JSON snapshot for the offline UI")
    common(export_parser)
    export_parser.add_argument("--out", default="frontend/data/snapshot.json")
    export_parser.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except DomainError as error:
        print(f"error: {error.detail}", file=sys.stderr)
        return 2
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
