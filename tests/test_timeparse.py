import unittest
from datetime import datetime, timedelta

from klok.timeparse import (TimeParseError, format_duration, parse_datetime,
                            parse_datetime_ex, parse_duration, parse_range,
                            round_duration, week_bounds)

# A Monday, so weekday arithmetic is easy to reason about.  astimezone()
# attaches the local zone without shifting the wall-clock reading, which
# keeps these assertions true wherever the suite runs.
NOW = datetime(2026, 8, 31, 14, 0).astimezone()


class DurationTests(unittest.TestCase):
    def test_bare_number_is_minutes(self):
        self.assertEqual(parse_duration("90"), timedelta(minutes=90))

    def test_compound_units(self):
        self.assertEqual(parse_duration("1h30m"), timedelta(hours=1, minutes=30))
        self.assertEqual(parse_duration("2 hours"), timedelta(hours=2))
        self.assertEqual(parse_duration("45s"), timedelta(seconds=45))
        self.assertEqual(parse_duration("1w"), timedelta(weeks=1))

    def test_clock_form(self):
        self.assertEqual(parse_duration("1:30"), timedelta(hours=1, minutes=30))
        self.assertEqual(parse_duration("0:00:30"), timedelta(seconds=30))

    def test_negative(self):
        self.assertEqual(parse_duration("-15m"), timedelta(minutes=-15))

    def test_rejects_nonsense(self):
        for text in ("", "soon", "5 bananas"):
            with self.assertRaises((TimeParseError, ValueError)):
                parse_duration(text)

    def test_formatting(self):
        self.assertEqual(format_duration(timedelta(hours=1, minutes=30)), "1h 30m")
        self.assertEqual(format_duration(timedelta(minutes=45)), "45m")
        self.assertEqual(format_duration(timedelta(minutes=45, seconds=30)), "45m 30s")
        self.assertEqual(format_duration(timedelta(hours=1, minutes=30), "clock"), "01:30:00")
        self.assertEqual(format_duration(timedelta(hours=1, minutes=30), "hours"), "1.50")

    def test_rounding_goes_up_to_the_next_slot(self):
        self.assertEqual(round_duration(timedelta(minutes=1), 15), timedelta(minutes=15))
        self.assertEqual(round_duration(timedelta(minutes=15), 15), timedelta(minutes=15))
        self.assertEqual(round_duration(timedelta(minutes=16), 15), timedelta(minutes=30))
        self.assertEqual(round_duration(timedelta(minutes=16), 0), timedelta(minutes=16))


class DateTimeTests(unittest.TestCase):
    def parse(self, text):
        return parse_datetime(text, NOW)

    def test_keywords(self):
        self.assertEqual(self.parse("now"), NOW)
        self.assertEqual(self.parse("today"), NOW.replace(hour=0, minute=0))
        self.assertEqual(self.parse("yesterday"), NOW.replace(day=30, hour=0, minute=0))
        self.assertEqual(self.parse("noon"), NOW.replace(hour=12, minute=0))

    def test_clock_forms(self):
        self.assertEqual(self.parse("09:30"), NOW.replace(hour=9, minute=30))
        self.assertEqual(self.parse("9am"), NOW.replace(hour=9, minute=0))
        self.assertEqual(self.parse("9:30pm"), NOW.replace(hour=21, minute=30))
        self.assertEqual(self.parse("12am"), NOW.replace(hour=0, minute=0))

    def test_relative(self):
        self.assertEqual(self.parse("-15min"), NOW - timedelta(minutes=15))
        self.assertEqual(self.parse("+1h30m"), NOW + timedelta(hours=1, minutes=30))
        self.assertEqual(self.parse("2 hours ago"), NOW - timedelta(hours=2))

    def test_weekdays(self):
        self.assertEqual(self.parse("monday"), NOW.replace(hour=0, minute=0))
        self.assertEqual(self.parse("last monday"), NOW.replace(day=24, hour=0, minute=0))
        self.assertEqual(self.parse("friday"), NOW.replace(day=28, hour=0, minute=0))

    def test_dates_and_datetimes(self):
        self.assertEqual(self.parse("2026-08-30"), NOW.replace(day=30, hour=0, minute=0))
        self.assertEqual(self.parse("2026-08-30 08:15"), NOW.replace(day=30, hour=8, minute=15))
        self.assertEqual(self.parse("2026-08-30T08:15:00"), NOW.replace(day=30, hour=8, minute=15))

    def test_granularity_is_reported(self):
        self.assertEqual(parse_datetime_ex("2026-08-30", NOW)[1], "day")
        self.assertEqual(parse_datetime_ex("09:30", NOW)[1], "instant")

    def test_rejects_nonsense(self):
        with self.assertRaises(TimeParseError):
            self.parse("whenever")


class RangeTests(unittest.TestCase):
    def range(self, tokens):
        return parse_range(tokens, NOW, "monday")

    def test_hints(self):
        start, end = self.range(":day")
        self.assertEqual((start.day, end.day), (31, 1))
        start, end = self.range(":yesterday")
        self.assertEqual((start.day, end.day), (30, 31))
        start, end = self.range(":week")
        self.assertEqual(start, NOW.replace(hour=0, minute=0))
        self.assertEqual(end - start, timedelta(days=7))

    def test_last_week_and_month(self):
        start, end = self.range(":lastweek")
        self.assertEqual(start, NOW.replace(day=24, hour=0, minute=0))
        start, end = self.range(":month")
        self.assertEqual((start.month, start.day, end.month), (8, 1, 9))
        start, end = self.range(":lastmonth")
        self.assertEqual((start.month, end.month), (7, 8))

    def test_explicit_span(self):
        start, end = self.range("09:00 to 11:30")
        self.assertEqual((start.hour, end.hour, end.minute), (9, 11, 30))

    def test_day_end_is_inclusive(self):
        start, end = self.range("2026-08-01 to 2026-08-31")
        self.assertEqual(start.day, 1)
        self.assertEqual(end, NOW.replace(month=9, day=1, hour=0, minute=0))

    def test_last_n_days(self):
        start, end = self.range("last 7 days")
        self.assertEqual(end - start, timedelta(days=7))

    def test_since(self):
        start, end = self.range("since monday")
        self.assertEqual(start, NOW.replace(hour=0, minute=0))

    def test_week_start_setting(self):
        monday, _ = week_bounds(NOW, "monday")
        sunday, _ = week_bounds(NOW, "sunday")
        self.assertEqual(monday.weekday(), 0)
        self.assertEqual(sunday.weekday(), 6)


if __name__ == "__main__":
    unittest.main()
