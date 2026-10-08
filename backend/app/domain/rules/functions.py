"""Standard library available to rule expressions.

Design notes
------------
* Every function is *pure* with respect to its arguments plus the frozen
  ``now``/calendar pair injected at construction time.  Determinism is what lets
  the dry-run endpoint promise "the same input yields the same decision".
* Coercion is explicit rather than Pythonic.  ``"high" == "HIGH"`` must be
  ``false``: a policy engine that silently case-folds strings produces tickets
  that route to the wrong team in production.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Callable

from ..errors import RuleEvaluationError
from .calendar import BusinessCalendar

#: Guards against a single ``matches()`` call becoming a regex DoS vector.
_REGEX_CACHE: dict[str, re.Pattern[str]] = {}
_MAX_REGEX_LEN = 200


def _as_datetime(value: Any, what: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuleEvaluationError(f"{what}: cannot read {value!r} as a timestamp") from exc
        if parsed.tzinfo is None:
            raise RuleEvaluationError(f"{what}: timestamp {value!r} has no timezone")
        return parsed
    raise RuleEvaluationError(f"{what}: expected a timestamp, got {type(value).__name__}")


def _as_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


class FunctionLibrary:
    """Callable surface bound to one ``now`` and one calendar."""

    def __init__(self, now: datetime, calendar: BusinessCalendar) -> None:
        self.now = now
        self.calendar = calendar
        self._functions: dict[str, Callable[[list[Any], str], Any]] = {
            "now": self._now,
            "age_hours": self._age_hours,
            "hours_since": self._hours_since,
            "hours_until": self._hours_until,
            "business_hours_between": self._business_hours_between,
            "start_of_day": self._start_of_day,
            "end_of_day": self._end_of_day,
            "add_business_hours": self._add_business_hours,
            "lower": self._lower,
            "upper": self._upper,
            "contains": self._contains,
            "startswith": self._startswith,
            "endswith": self._endswith,
            "matches": self._matches,
            "len": self._len,
            "coalesce": self._coalesce,
            "int": self._int,
            "float": self._float,
            "str": self._str,
            "min": self._min,
            "max": self._max,
            "abs": self._abs,
            "round": self._round,
            "date_part": self._date_part,
            "weekday": self._weekday,
            "is_business_day": self._is_business_day,
            "in_quiet_hours": self._in_quiet_hours,
        }

    def names(self) -> list[str]:
        return sorted(self._functions)

    def call(self, name: str, args: list[Any], source: str = "") -> Any:
        handler = self._functions.get(name)
        if handler is None:
            raise RuleEvaluationError(f"unknown function '{name}'")
        try:
            return handler(args, source)
        except RuleEvaluationError:
            raise
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise RuleEvaluationError(f"{name}(): {exc}") from exc

    # -- time ------------------------------------------------------------
    def _now(self, args: list[Any], _src: str) -> datetime:
        self._arity("now", args, 0)
        return self.now

    def _age_hours(self, args: list[Any], _src: str) -> float:
        self._arity("age_hours", args, 0, 1)
        moment = self.now if not args else _as_datetime(args[0], "age_hours")
        reference = _as_datetime(args[1], "age_hours") if len(args) > 1 else self.now
        return round((reference - moment).total_seconds() / 3600.0, 4)

    def _hours_since(self, args: list[Any], _src: str) -> float:
        self._arity("hours_since", args, 1, 2)
        start = _as_datetime(args[0], "hours_since")
        end = _as_datetime(args[1], "hours_since") if len(args) > 1 else self.now
        return round((end - start).total_seconds() / 3600.0, 4)

    def _hours_until(self, args: list[Any], _src: str) -> float:
        self._arity("hours_until", args, 1, 2)
        target = _as_datetime(args[0], "hours_until")
        base = _as_datetime(args[1], "hours_until") if len(args) > 1 else self.now
        return round((target - base).total_seconds() / 3600.0, 4)

    def _business_hours_between(self, args: list[Any], _src: str) -> float:
        self._arity("business_hours_between", args, 2)
        return self.calendar.business_hours_between(
            _as_datetime(args[0], "business_hours_between"),
            _as_datetime(args[1], "business_hours_between"),
        )

    def _start_of_day(self, args: list[Any], _src: str) -> datetime:
        self._arity("start_of_day", args, 0, 1)
        moment = _as_datetime(args[0], "start_of_day") if args else self.now
        local = moment.astimezone(self.calendar.tzinfo)
        return datetime.combine(local.date(), self.calendar.day_start, tzinfo=self.calendar.tzinfo)

    def _end_of_day(self, args: list[Any], _src: str) -> datetime:
        self._arity("end_of_day", args, 0, 1)
        moment = _as_datetime(args[0], "end_of_day") if args else self.now
        local = moment.astimezone(self.calendar.tzinfo)
        return datetime.combine(local.date(), self.calendar.day_end, tzinfo=self.calendar.tzinfo)

    def _add_business_hours(self, args: list[Any], _src: str) -> datetime:
        self._arity("add_business_hours", args, 2)
        return self.calendar.add_business_hours(
            _as_datetime(args[0], "add_business_hours"),
            float(self._number(args[1], "add_business_hours")),
        )

    # -- strings ---------------------------------------------------------
    def _lower(self, args: list[Any], _src: str) -> str:
        self._arity("lower", args, 1)
        return _as_str(args[0]).lower()

    def _upper(self, args: list[Any], _src: str) -> str:
        self._arity("upper", args, 1)
        return _as_str(args[0]).upper()

    def _contains(self, args: list[Any], _src: str) -> bool:
        self._arity("contains", args, 2)
        return _as_str(args[1]) in _as_str(args[0])

    def _startswith(self, args: list[Any], _src: str) -> bool:
        self._arity("startswith", args, 2)
        return _as_str(args[0]).startswith(_as_str(args[1]))

    def _endswith(self, args: list[Any], _src: str) -> bool:
        self._arity("endswith", args, 2)
        return _as_str(args[0]).endswith(_as_str(args[1]))

    def _matches(self, args: list[Any], source: str) -> bool:
        self._arity("matches", args, 2)
        pattern = _as_str(args[0])
        subject = _as_str(args[1])
        if len(pattern) > _MAX_REGEX_LEN:
            raise RuleEvaluationError(f"matches(): pattern longer than {_MAX_REGEX_LEN} characters")
        compiled = _REGEX_CACHE.get(pattern)
        if compiled is None:
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise RuleEvaluationError(f"matches(): invalid pattern {pattern!r} ({exc})") from exc
            if len(_REGEX_CACHE) < 512:
                _REGEX_CACHE[pattern] = compiled
        return compiled.search(subject) is not None

    def _len(self, args: list[Any], _src: str) -> int:
        self._arity("len", args, 1)
        value = args[0]
        if isinstance(value, (str, list, tuple, dict, set)):
            return len(value)
        if isinstance(value, bool) or value is None:
            return 0
        raise RuleEvaluationError(f"len(): cannot measure {type(value).__name__}")

    def _coalesce(self, args: list[Any], _src: str) -> Any:
        self._arity("coalesce", args, 1, 16)
        for value in args:
            if value is not None and value != "":
                return value
        return None

    # -- numbers ---------------------------------------------------------
    @staticmethod
    def _number(value: Any, where: str) -> float:
        if isinstance(value, bool):
            raise RuleEvaluationError(f"{where}: expected a number, got a boolean")
        if isinstance(value, (int, float)):
            return float(value)
        raise RuleEvaluationError(f"{where}: expected a number, got {type(value).__name__}")

    def _int(self, args: list[Any], _src: str) -> int:
        self._arity("int", args, 1)
        value = args[0]
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip():
            try:
                return int(float(value))
            except ValueError as exc:
                raise RuleEvaluationError(f"int(): cannot parse {value!r}") from exc
        raise RuleEvaluationError(f"int(): cannot convert {type(value).__name__}")

    def _float(self, args: list[Any], _src: str) -> float:
        self._arity("float", args, 1)
        value = args[0]
        if isinstance(value, bool):
            raise RuleEvaluationError("float(): expected a number, got a boolean")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value.strip():
            try:
                return float(value)
            except ValueError as exc:
                raise RuleEvaluationError(f"float(): cannot parse {value!r}") from exc
        raise RuleEvaluationError(f"float(): cannot convert {type(value).__name__}")

    def _str(self, args: list[Any], _src: str) -> str:
        self._arity("str", args, 1)
        value = args[0]
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    def _min(self, args: list[Any], _src: str) -> Any:
        return self._extreme("min", args, min)

    def _max(self, args: list[Any], _src: str) -> Any:
        return self._extreme("max", args, max)

    def _extreme(self, name: str, args: list[Any], fn: Callable[..., Any]) -> Any:
        self._arity(name, args, 1, 32)
        values = list(args[0]) if len(args) == 1 and isinstance(args[0], (list, tuple)) else list(args)
        if not values:
            raise RuleEvaluationError(f"{name}(): needs at least one value")
        return fn(values)

    def _abs(self, args: list[Any], _src: str) -> float:
        self._arity("abs", args, 1)
        return abs(self._number(args[0], "abs"))

    def _round(self, args: list[Any], _src: str) -> float:
        self._arity("round", args, 1, 2)
        digits = int(self._number(args[1], "round")) if len(args) > 1 else 0
        return round(self._number(args[0], "round"), digits)

    # -- calendar --------------------------------------------------------
    def _date_part(self, args: list[Any], _src: str) -> int:
        self._arity("date_part", args, 2)
        part = _as_str(args[0]).lower()
        moment = _as_datetime(args[1], "date_part").astimezone(self.calendar.tzinfo)
        parts = {
            "year": moment.year,
            "month": moment.month,
            "day": moment.day,
            "hour": moment.hour,
            "minute": moment.minute,
            "weekday": moment.weekday(),
        }
        if part not in parts:
            raise RuleEvaluationError(f"date_part(): unknown part {part!r}")
        return parts[part]

    def _weekday(self, args: list[Any], _src: str) -> int:
        self._arity("weekday", args, 0, 1)
        moment = _as_datetime(args[0], "weekday") if args else self.now
        return moment.astimezone(self.calendar.tzinfo).weekday()

    def _is_business_day(self, args: list[Any], _src: str) -> bool:
        self._arity("is_business_day", args, 0, 1)
        if not args:
            day = self.now.astimezone(self.calendar.tzinfo).date()
        else:
            value = args[0]
            if isinstance(value, date) and not isinstance(value, datetime):
                day = value
            else:
                day = _as_datetime(value, "is_business_day").astimezone(self.calendar.tzinfo).date()
        return self.calendar.is_business_day(day)

    def _in_quiet_hours(self, args: list[Any], _src: str) -> bool:
        self._arity("in_quiet_hours", args, 0, 1)
        moment = _as_datetime(args[0], "in_quiet_hours") if args else self.now
        return self.calendar.is_quiet_hours(moment)

    # -- guards ----------------------------------------------------------
    @staticmethod
    def _arity(name: str, args: list[Any], low: int, high: int | None = None) -> None:
        high = low if high is None else high
        if not low <= len(args) <= high:
            expected = str(low) if low == high else f"{low}..{high}"
            raise RuleEvaluationError(f"{name}() takes {expected} argument(s), got {len(args)}")


#: Plainly documented for the DSL reference page.
SIGNATURES: dict[str, str] = {
    "now()": "current instant (frozen for the whole evaluation)",
    "age_hours()": "hours since ticket.created_at",
    "hours_since(ts[, ref])": "wall-clock hours between two instants",
    "hours_until(ts[, ref])": "wall-clock hours until an instant (negative if past)",
    "business_hours_between(a, b)": "working hours inside a range, holidays excluded",
    "start_of_day([ts])": "opening time of the local day",
    "end_of_day([ts])": "closing time of the local day",
    "add_business_hours(ts, h)": "advance h working hours from ts",
    "lower/upper(s)": "ASCII case folding",
    "contains(s, sub)": "substring test",
    "startswith/endswith(s, affix)": "prefix / suffix test",
    "matches(pattern, s)": "regular-expression search (length capped)",
    "len(x)": "length of string/list/object",
    "coalesce(a, b, ...)": "first non-empty argument",
    "int/float/str(x)": "explicit coercion",
    "min/max(a, b, ...)": "extreme of the arguments or of a list",
    "abs/round(x[, n])": "numeric helpers",
    "date_part('hour', ts)": "year|month|day|hour|minute|weekday in local time",
    "weekday([ts])": "0=Monday .. 6=Sunday",
    "is_business_day([ts])": "false on holidays, true on 调休 make-up days",
    "in_quiet_hours([ts])": "true outside working hours",
}


def default_calendar() -> BusinessCalendar:
    return BusinessCalendar()
