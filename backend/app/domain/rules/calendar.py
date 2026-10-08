"""Business-hour calendar used by everything SLA related.

Two details separate a real service desk from a demo:

* **Holidays subtract time** — a 4-hour SLA opened at 17:00 on the day before a
  week-long national holiday must not be reported as breached.
* **Make-up workdays add time** — Chinese holidays come with 调休 weekends that
  *are* working days.  A calendar that only knows "weekends are closed" will get
  every Spring Festival SLA wrong.

Both are data here, not code, so an operator can load next year's published
holiday schedule without a release.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone

WORKING_DAYS = 0b0011111  # Monday..Friday as bits 0..6


@dataclass(frozen=True, slots=True)
class BusinessCalendar:
    """A timezone + working-hours + holiday model.

    Instants passed in must be timezone-aware; everything is normalised to
    ``tz_offset_minutes`` before wall-clock rules are applied.
    """

    tz_offset_minutes: int = 480  # UTC+8 / Asia/Shanghai
    working_days: int = WORKING_DAYS
    day_start: time = time(9, 0)
    day_end: time = time(18, 0)
    #: Dates that are closed even though ``working_days`` says otherwise.
    holidays: frozenset[date] = field(default_factory=frozenset)
    #: Dates that are open even though ``working_days`` says otherwise (调休).
    make_up_workdays: frozenset[date] = field(default_factory=frozenset)

    # -- construction ----------------------------------------------------
    @property
    def tzinfo(self) -> timezone:
        return timezone(timedelta(minutes=self.tz_offset_minutes))

    @classmethod
    def from_dict(cls, data: dict) -> "BusinessCalendar":
        def parse_time(raw: object, fallback: time) -> time:
            if isinstance(raw, str) and ":" in raw:
                hour, _, minute = raw.partition(":")
                return time(int(hour), int(minute))
            return fallback

        def parse_dates(raw: object) -> frozenset[date]:
            out: set[date] = set()
            for item in raw or ():  # type: ignore[union-attr]
                if isinstance(item, str) and item.strip():
                    out.add(date.fromisoformat(item.strip()))
            return frozenset(out)

        days = data.get("working_days", ["mon", "tue", "wed", "thu", "fri"])
        mask = 0
        names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        for name in days:
            lowered = str(name).strip().lower()[:3]
            if lowered in names:
                mask |= 1 << names.index(lowered)
        return cls(
            tz_offset_minutes=int(data.get("tz_offset_minutes", 480)),
            working_days=mask or WORKING_DAYS,
            day_start=parse_time(data.get("day_start"), time(9, 0)),
            day_end=parse_time(data.get("day_end"), time(18, 0)),
            holidays=parse_dates(data.get("holidays")),
            make_up_workdays=parse_dates(data.get("make_up_workdays")),
        )

    def with_holidays(self, holidays: frozenset[date]) -> "BusinessCalendar":
        return replace(self, holidays=holidays)

    # -- primitives ------------------------------------------------------
    def is_business_day(self, day: date) -> bool:
        if day in self.holidays:
            return False
        if day in self.make_up_workdays:
            return True
        return bool(self.working_days & (1 << day.weekday()))

    def _intervals(self, day: date) -> list[tuple[datetime, datetime]]:
        if not self.is_business_day(day):
            return []
        tz = self.tzinfo
        start = datetime.combine(day, self.day_start, tzinfo=tz)
        end = datetime.combine(day, self.day_end, tzinfo=tz)
        if end <= start:  # overnight shift, e.g. 22:00 -> 06:00 next day
            end += timedelta(days=1)
        return [(start, end)]

    def _window(self, start_day: date, days: int = 400):
        """Yield working intervals lazily, so callers can stop early."""
        for offset in range(days):
            yield from self._intervals(start_day + timedelta(days=offset))

    def next_opening(self, moment: datetime) -> datetime:
        """First business instant at or after ``moment``."""
        local = moment.astimezone(self.tzinfo)
        day = local.date()
        for offset in range(0, 400):
            for start, end in self._intervals(day + timedelta(days=offset)):
                if local < start:
                    return start
                if start <= local < end:
                    return local
        return local  # pragma: no cover - 400 consecutive closed days is not realistic

    # -- the interesting bits -------------------------------------------
    def business_hours_between(self, start: datetime, end: datetime) -> float:
        """Business hours in the half-open interval (start, end].

        Returns ``0.0`` when ``end <= start`` and never counts closed time.
        """
        if end <= start:
            return 0.0
        tz = self.tzinfo
        a = start.astimezone(tz)
        b = end.astimezone(tz)
        total = 0.0
        for lo, hi in self._window(a.date()):
            if hi <= a:
                continue
            if lo >= b:
                break  # every later interval starts even later
            overlap_start = max(lo, a)
            overlap_end = min(hi, b)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds() / 3600.0
        return round(total, 6)

    def add_business_hours(self, start: datetime, hours: float) -> datetime:
        """Advance ``hours`` of *business* time from ``start``."""
        if hours <= 0:
            return self.next_opening(start)
        local = self.next_opening(start)
        remaining = float(hours)
        day = local.date()
        for offset in range(0, 400):
            for lo, hi in self._intervals(day + timedelta(days=offset)):
                if hi <= local:
                    continue
                available = (hi - max(lo, local)).total_seconds() / 3600.0
                if remaining <= available:
                    return max(lo, local) + timedelta(hours=remaining)
                remaining -= available
                local = hi
        return local  # pragma: no cover - defensive

    def is_quiet_hours(self, moment: datetime) -> bool:
        """True outside working hours — used by the on-call notification policy.

        The previous day is included because an overnight shift (22:00 → 06:00)
        belongs to the day it *started*: 05:00 Tuesday is still Monday's shift.
        """
        local = moment.astimezone(self.tzinfo)
        for day in (local.date() - timedelta(days=1), local.date()):
            for lo, hi in self._intervals(day):
                if lo <= local < hi:
                    return False
        return True

    def elapsed_business_hours(self, start: datetime, end: datetime) -> float:
        return self.business_hours_between(start, end)

    def describe(self) -> str:
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        days = [names[i] for i in range(7) if self.working_days & (1 << i)]
        return (
            f"{'/'.join(days)} {self.day_start:%H:%M}-{self.day_end:%H:%M} "
            f"UTC{self.tz_offset_minutes // 60:+03d}:00 "
            f"({len(self.holidays)} holidays, {len(self.make_up_workdays)} make-up days)"
        )


UTC = timezone.utc
