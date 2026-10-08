"""Business-calendar tests, including the Chinese holiday/调休 edge cases."""

from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta, timezone

from app.domain.rules.calendar import BusinessCalendar

CN = timezone(timedelta(hours=8))


def cn(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=CN)


#: The published 2025 National Day holiday run plus its make-up weekend.
NATIONAL_DAY = BusinessCalendar(
    holidays=frozenset(date(2025, 10, d) for d in range(1, 9)),
    make_up_workdays=frozenset({date(2025, 9, 28), date(2025, 10, 11)}),
)


class WorkingDayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = BusinessCalendar()

    def test_weekdays_are_business_days(self) -> None:
        self.assertTrue(self.calendar.is_business_day(date(2026, 3, 2)))  # Monday
        self.assertTrue(self.calendar.is_business_day(date(2026, 3, 6)))  # Friday

    def test_weekend_is_closed(self) -> None:
        self.assertFalse(self.calendar.is_business_day(date(2026, 3, 7)))  # Saturday
        self.assertFalse(self.calendar.is_business_day(date(2026, 3, 8)))  # Sunday

    def test_holiday_beats_a_weekday(self) -> None:
        self.assertFalse(NATIONAL_DAY.is_business_day(date(2025, 10, 1)))  # Wednesday, closed

    def test_make_up_workday_beats_a_weekend(self) -> None:
        # 调休: Saturday 2025-10-11 was a working day, so SLA time keeps running.
        self.assertTrue(NATIONAL_DAY.is_business_day(date(2025, 10, 11)))

    def test_custom_shifts_can_work_six_days(self) -> None:
        six_day = BusinessCalendar(working_days=0b0111111)
        self.assertTrue(six_day.is_business_day(date(2026, 3, 7)))

    def test_from_dict_reads_the_config_shape(self) -> None:
        calendar = BusinessCalendar.from_dict(
            {
                "tz_offset_minutes": 480,
                "working_days": ["mon", "tue", "wed", "thu", "fri", "sat"],
                "day_start": "08:30",
                "day_end": "17:30",
                "holidays": ["2026-01-01"],
                "make_up_workdays": ["2026-01-04"],
            }
        )
        self.assertEqual(calendar.day_start.hour, 8)
        self.assertEqual(calendar.day_start.minute, 30)
        self.assertTrue(calendar.is_business_day(date(2026, 1, 3)))  # Saturday
        self.assertFalse(calendar.is_business_day(date(2026, 1, 1)))
        self.assertTrue(calendar.is_business_day(date(2026, 1, 4)))  # Sunday, 调休

    def test_from_dict_tolerates_garbage(self) -> None:
        calendar = BusinessCalendar.from_dict({"working_days": [], "day_start": "oops", "holidays": [""]})
        self.assertEqual(calendar.working_days, 0b0011111)
        self.assertEqual(calendar.day_start.hour, 9)

    def test_describe_is_human_readable(self) -> None:
        text = BusinessCalendar().describe()
        self.assertIn("Mon/Tue/Wed/Thu/Fri", text)
        self.assertIn("09:00-18:00", text)


class BusinessHoursTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = BusinessCalendar()

    def test_within_a_single_day(self) -> None:
        self.assertEqual(self.calendar.business_hours_between(cn(2026, 3, 2, 10), cn(2026, 3, 2, 12)), 2.0)

    def test_duration_is_clipped_to_opening_hours(self) -> None:
        # 08:00 -> 10:00 is two wall-clock hours but only one business hour.
        self.assertEqual(self.calendar.business_hours_between(cn(2026, 3, 2, 8), cn(2026, 3, 2, 10)), 1.0)

    def test_night_and_weekend_add_nothing(self) -> None:
        self.assertEqual(self.calendar.business_hours_between(cn(2026, 3, 2, 22), cn(2026, 3, 3, 6)), 0.0)
        self.assertEqual(self.calendar.business_hours_between(cn(2026, 3, 7), cn(2026, 3, 9)), 0.0)

    def test_spans_the_weekend_correctly(self) -> None:
        # Fri 17:00 -> Mon 11:00: 1h Friday + 2h Monday.
        self.assertEqual(
            self.calendar.business_hours_between(cn(2026, 3, 6, 17), cn(2026, 3, 9, 11)), 3.0
        )

    def test_reversed_and_empty_ranges_are_zero(self) -> None:
        self.assertEqual(self.calendar.business_hours_between(cn(2026, 3, 2, 12), cn(2026, 3, 2, 10)), 0.0)
        self.assertEqual(self.calendar.business_hours_between(cn(2026, 3, 2, 12), cn(2026, 3, 2, 12)), 0.0)

    def test_holidays_are_subtracted(self) -> None:
        # Mon 2025-09-29 -> Mon 2025-10-13 across the 8-day National Day run:
        # two full days before, two after, plus the 调休 Saturday — which counts
        # as a *full* working day, not a shortened one.  The 10-13 end is
        # exclusive and contributes nothing.
        total = NATIONAL_DAY.business_hours_between(cn(2025, 9, 29), cn(2025, 10, 13))
        self.assertEqual(total, 45.0)

    def test_make_up_saturday_is_a_full_working_day(self) -> None:
        self.assertEqual(
            NATIONAL_DAY.business_hours_between(cn(2025, 10, 11), cn(2025, 10, 12)), 9.0
        )

    def test_the_range_end_is_exclusive(self) -> None:
        # Reaching 09:00 on a working day must add nothing.
        self.assertEqual(
            NATIONAL_DAY.business_hours_between(cn(2025, 9, 29), cn(2025, 9, 30, 9)), 9.0
        )

    def test_make_up_weekend_is_counted(self) -> None:
        self.assertEqual(NATIONAL_DAY.business_hours_between(cn(2025, 10, 11), cn(2025, 10, 11, 12)), 3.0)

    def test_holiday_day_contributes_nothing(self) -> None:
        self.assertEqual(NATIONAL_DAY.business_hours_between(cn(2025, 10, 1), cn(2025, 10, 2)), 0.0)


class AddBusinessHoursTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = BusinessCalendar()

    def test_starts_counting_at_the_next_opening(self) -> None:
        # Opened 03:00, 2 business hours -> 11:00 the same day.
        self.assertEqual(self.calendar.add_business_hours(cn(2026, 3, 2, 3), 2), cn(2026, 3, 2, 11))

    def test_rolls_over_to_the_next_business_day(self) -> None:
        self.assertEqual(self.calendar.add_business_hours(cn(2026, 3, 2, 10), 12), cn(2026, 3, 3, 13))

    def test_rolls_over_a_weekend(self) -> None:
        # Friday 17:00 + 4 business hours -> Monday 12:00.
        self.assertEqual(self.calendar.add_business_hours(cn(2026, 3, 6, 17), 4), cn(2026, 3, 9, 12))

    def test_zero_hours_lands_on_the_next_opening(self) -> None:
        self.assertEqual(self.calendar.add_business_hours(cn(2026, 3, 7, 12), 0), cn(2026, 3, 9, 9))

    def test_national_day_push(self) -> None:
        # Opened 2025-09-30 17:00 with a 4h target: 1h that evening, 3h on the
        # first working day after the holiday (2025-10-09, since 10-01..10-08 are closed).
        self.assertEqual(NATIONAL_DAY.add_business_hours(cn(2025, 9, 30, 17), 4), cn(2025, 10, 9, 12))

    def test_round_trip_add_then_measure(self) -> None:
        start = cn(2026, 3, 2, 10, 30)
        due = self.calendar.add_business_hours(start, 7.5)
        self.assertAlmostEqual(self.calendar.business_hours_between(start, due), 7.5, places=6)


class QuietHoursTests(unittest.TestCase):
    def test_inside_and_outside_working_hours(self) -> None:
        calendar = BusinessCalendar()
        self.assertFalse(calendar.is_quiet_hours(cn(2026, 3, 2, 10)))
        self.assertTrue(calendar.is_quiet_hours(cn(2026, 3, 2, 20)))
        self.assertTrue(calendar.is_quiet_hours(cn(2026, 3, 7, 10)))

    def test_overnight_shift_is_supported(self) -> None:
        night = BusinessCalendar(day_start=time(22, 0), day_end=time(6, 0))
        # 23:00 belongs to the shift that opened the same evening …
        self.assertFalse(night.is_quiet_hours(cn(2026, 3, 2, 23)))
        # … and 05:00 still belongs to the shift that opened the previous evening.
        self.assertFalse(night.is_quiet_hours(cn(2026, 3, 3, 5)))
        self.assertTrue(night.is_quiet_hours(cn(2026, 3, 3, 12)))

    def test_overnight_shift_measures_across_midnight(self) -> None:
        night = BusinessCalendar(day_start=time(22, 0), day_end=time(6, 0))
        self.assertEqual(night.business_hours_between(cn(2026, 3, 2, 23), cn(2026, 3, 3, 2)), 3.0)
        self.assertEqual(night.business_hours_between(cn(2026, 3, 2, 12), cn(2026, 3, 3, 2)), 4.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
