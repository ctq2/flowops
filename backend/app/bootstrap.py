"""Composition root.

The only module that knows how the layers are wired together.  Tests import the
factories below to build a fully-wired service over an in-memory store; the CLI
does the same over SQLite.  Nothing else in the codebase constructs adapters.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .adapters.persistence.memory import InMemoryStore
from .adapters.persistence.sqlite_store import SqliteStore
from .application.ports.store import TicketStore
from .application.services.tickets import ListEventSink, TicketService
from .config.settings import Settings
from .domain.errors import ValidationError
from .domain.rules.engine import CompiledPolicy, Policy
from .domain.ticket import Ticket


def load_policy(settings: Settings) -> tuple[Policy, dict[str, Any]]:
    """Load the policy document, merging the shared holiday calendar into it.

    Keeping holidays in their own file means next year's State Council notice is
    a data edit, not a policy rewrite (and therefore not a policy review).
    """
    if not settings.policy_path.exists():
        raise ValidationError(f"policy file not found: {settings.policy_path}")
    document = json.loads(settings.policy_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValidationError("policy root must be a JSON object")

    if settings.calendar_path.exists():
        holidays = json.loads(settings.calendar_path.read_text(encoding="utf-8"))
        calendar = dict(document.get("calendar") or {})
        calendar.setdefault("tz_offset_minutes", holidays.get("tz_offset_minutes", 480))
        calendar.setdefault("working_days", holidays.get("working_days", []))
        working_hours = holidays.get("working_hours") or {}
        calendar.setdefault("day_start", working_hours.get("day_start", "09:00"))
        calendar.setdefault("day_end", working_hours.get("day_end", "18:00"))
        merged_holidays = set(calendar.get("holidays") or ())
        merged_make_up = set(calendar.get("make_up_workdays") or ())
        for year_data in (holidays.get("years") or {}).values():
            merged_holidays.update(year_data.get("holidays") or ())
            merged_make_up.update(year_data.get("make_up_workdays") or ())
        calendar["holidays"] = sorted(merged_holidays)
        calendar["make_up_workdays"] = sorted(merged_make_up)
        document["calendar"] = calendar

    return Policy.from_dict(document), document


def build_store(settings: Settings, *, tickets: list[Ticket] | None = None) -> TicketStore:
    if settings.store == "memory":
        return InMemoryStore(tickets)
    settings.database.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(settings.database)
    for ticket in tickets or ():
        if store.get(ticket.id) is None:
            store.add(ticket)
    return store


def build_service(
    settings: Settings,
    *,
    store: TicketStore | None = None,
    tickets: list[Ticket] | None = None,
    events: ListEventSink | None = None,
) -> tuple[TicketService, CompiledPolicy, dict[str, Any], TicketStore]:
    policy, document = load_policy(settings)
    actual_store = store or build_store(settings, tickets=tickets)
    service = TicketService(actual_store, policy.compiled, events=events)
    return service, policy.compiled, document, actual_store


def holiday_summary(settings: Settings) -> dict[str, Any]:
    """Read the holiday file for the CLI's ``calendar`` command."""
    if not settings.calendar_path.exists():
        return {"error": f"calendar file not found: {settings.calendar_path}"}
    data = json.loads(settings.calendar_path.read_text(encoding="utf-8"))
    years: dict[str, dict[str, int]] = {}
    for year, payload in (data.get("years") or {}).items():
        years[year] = {
            "holidays": len(payload.get("holidays") or []),
            "make_up_workdays": len(payload.get("make_up_workdays") or []),
        }
    return {
        "timezone": data.get("timezone"),
        "tz_offset_minutes": data.get("tz_offset_minutes"),
        "working_hours": data.get("working_hours"),
        "days": data.get("working_days"),
        "years": years,
    }


def upcoming_holidays(settings: Settings, *, after: date, limit: int = 10) -> list[dict[str, str]]:
    """The next ``limit`` closed dates — the console shows these to operators."""
    if not settings.calendar_path.exists():
        return []
    data = json.loads(settings.calendar_path.read_text(encoding="utf-8"))
    out: list[dict[str, str]] = []
    for year, payload in sorted((data.get("years") or {}).items()):
        for day in sorted(payload.get("holidays") or []):
            parsed = date.fromisoformat(day)
            if parsed >= after:
                out.append({"date": day, "kind": "holiday"})
        for day in sorted(payload.get("make_up_workdays") or []):
            parsed = date.fromisoformat(day)
            if parsed >= after:
                out.append({"date": day, "kind": "make_up_workday"})
    out.sort(key=lambda item: item["date"])
    return out[:limit]


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
