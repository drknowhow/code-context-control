import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from services import cron_expr as ce

UTC = timezone.utc


def at(y, m, d, hh=0, mm=0, tz=UTC):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


class TestParse(unittest.TestCase):
    def test_star_fields_are_unrestricted(self):
        spec = ce.parse("* * * * *")
        self.assertEqual(len(spec.minutes), 60)
        self.assertEqual(len(spec.hours), 24)
        self.assertFalse(spec.day_restricted)
        self.assertFalse(spec.weekday_restricted)

    def test_lists_ranges_steps_and_names(self):
        spec = ce.parse("0,30 9-17/4 1,15 jan,JUL mon-fri")
        self.assertEqual(spec.minutes, frozenset({0, 30}))
        self.assertEqual(spec.hours, frozenset({9, 13, 17}))
        self.assertEqual(spec.days, frozenset({1, 15}))
        self.assertEqual(spec.months, frozenset({1, 7}))
        self.assertEqual(spec.weekdays, frozenset({1, 2, 3, 4, 5}))

    def test_seven_is_sunday_and_aliases_expand(self):
        self.assertEqual(ce.parse("0 0 * * 7").weekdays, frozenset({0}))
        self.assertEqual(ce.parse("@weekly").weekdays, frozenset({0}))
        self.assertEqual(ce.parse("@hourly").minutes, frozenset({0}))

    def test_rejects_bad_expressions(self):
        for expr in ("* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *",
                     "*/0 * * * *", "5-1 * * * *", "x * * * *", ""):
            with self.assertRaises(ValueError, msg=expr):
                ce.parse(expr)


class TestNextAfter(unittest.TestCase):
    def test_strictly_after_and_minute_floor(self):
        spec = ce.parse("*/15 * * * *")
        base = at(2026, 10, 13, 9, 0) + timedelta(seconds=20)
        self.assertEqual(ce.next_after(spec, base), at(2026, 10, 13, 9, 15))
        self.assertEqual(ce.next_after(spec, at(2026, 10, 13, 9, 15)), at(2026, 10, 13, 9, 30))

    def test_weekdays_skip_the_weekend(self):
        spec = ce.parse("0 9 * * 1-5")
        friday = at(2026, 10, 16, 10, 0)
        self.assertEqual(ce.next_after(spec, friday), at(2026, 10, 19, 9, 0))

    def test_day_and_weekday_both_restricted_is_or(self):
        spec = ce.parse("0 0 13 * 5")
        self.assertEqual(ce.next_after(spec, at(2026, 10, 10)), at(2026, 10, 13))
        self.assertEqual(ce.next_after(spec, at(2026, 10, 13)), at(2026, 10, 16))

    def test_month_end_rollover(self):
        spec = ce.parse("0 0 31 * *")
        self.assertEqual(ce.next_after(spec, at(2026, 2, 1)), at(2026, 3, 31))

    def test_impossible_date_returns_none(self):
        self.assertIsNone(ce.next_after(ce.parse("0 0 30 2 *"), at(2026, 1, 1)))

    def test_local_wall_clock_in_named_zone(self):
        ny = ZoneInfo("America/New_York")
        spec = ce.parse("0 9 * * *")
        got = ce.next_after(spec, at(2026, 1, 5, 12, 0), tz=ny)
        self.assertEqual(got, at(2026, 1, 5, 14, 0))
        self.assertEqual(got.tzinfo, UTC)

    def test_dst_gap_fires_once_at_shifted_instant(self):
        ny = ZoneInfo("America/New_York")
        spec = ce.parse("30 2 * * *")
        first = ce.next_after(spec, at(2026, 3, 7, 12, 0), tz=ny)
        second = ce.next_after(spec, first, tz=ny)
        self.assertEqual(first, at(2026, 3, 8, 7, 30))
        self.assertEqual(second, at(2026, 3, 9, 6, 30))

    def test_dst_overlap_fires_once(self):
        ny = ZoneInfo("America/New_York")
        spec = ce.parse("30 1 * * *")
        first = ce.next_after(spec, at(2026, 10, 31, 12, 0), tz=ny)
        second = ce.next_after(spec, first, tz=ny)
        self.assertEqual(first, at(2026, 11, 1, 5, 30))
        self.assertEqual(second, at(2026, 11, 2, 6, 30))

    def test_naive_after_is_rejected(self):
        with self.assertRaises(ValueError):
            ce.next_after(ce.parse("* * * * *"), datetime(2026, 1, 1))


class TestWhenShapes(unittest.TestCase):
    def test_presets_build_cron(self):
        self.assertEqual(ce.preset_to_cron("daily", "09:05"), "5 9 * * *")
        self.assertEqual(ce.preset_to_cron("weekdays", "18:00"), "0 18 * * 1-5")
        self.assertEqual(ce.preset_to_cron("hourly", "00:15"), "15 * * * *")
        self.assertEqual(ce.preset_to_cron("monthly", "08:00"), "0 8 1 * *")
        self.assertEqual(ce.preset_to_cron("weekly", "07:00", ["fri", "Mon"]), "0 7 * * 1,5")
        with self.assertRaises(ValueError):
            ce.preset_to_cron("fortnightly")
        with self.assertRaises(ValueError):
            ce.preset_to_cron("daily", "9am")

    def test_validate_when_normalises_each_type(self):
        self.assertEqual(ce.validate_when({"type": "cron", "expr": "0 9 * * *"}),
                         {"type": "cron", "expr": "0 9 * * *", "tz": "local"})
        self.assertEqual(ce.validate_when({"type": "every", "seconds": "600"}),
                         {"type": "every", "seconds": 600})
        self.assertEqual(ce.validate_when({"type": "at", "when": "2026-10-14T09:00Z"})["when"],
                         "2026-10-14T09:00:00+00:00")
        preset = ce.validate_when({"type": "preset", "preset": "weekdays", "at": "09:00"})
        self.assertEqual(preset["expr"], "0 9 * * 1-5")

    def test_validate_when_rejects(self):
        bad = [{"type": "weekly"}, {"type": "every", "seconds": 5}, {"type": "at", "when": "soon"},
               {"type": "cron", "expr": "0 9 * * *", "tz": "Mars/Olympus"}, "0 9 * * *"]
        for when in bad:
            with self.assertRaises(ValueError, msg=repr(when)):
                ce.validate_when(when)

    def test_next_fire_by_type(self):
        now = at(2026, 10, 13, 9, 0)
        self.assertEqual(ce.next_fire({"type": "every", "seconds": 600}, now), at(2026, 10, 13, 9, 10))
        self.assertEqual(ce.next_fire({"type": "cron", "expr": "0 10 * * *", "tz": "UTC"}, now),
                         at(2026, 10, 13, 10, 0))
        self.assertEqual(ce.next_fire({"type": "at", "when": "2026-10-14T00:00:00+00:00"}, now),
                         at(2026, 10, 14))
        self.assertIsNone(ce.next_fire({"type": "at", "when": "2026-10-13T00:00:00+00:00"}, now))

    def test_preview_counts_and_stops(self):
        fires = ce.preview({"type": "cron", "expr": "0 * * * *", "tz": "UTC"}, at(2026, 1, 1, 0, 30), 3)
        self.assertEqual([f.hour for f in fires], [1, 2, 3])
        once = ce.preview({"type": "at", "when": "2026-01-02T00:00:00+00:00"}, at(2026, 1, 1), 5)
        self.assertEqual(len(once), 1)

    def test_resolve_tz(self):
        self.assertEqual(ce.resolve_tz("UTC"), UTC)
        self.assertIsNotNone(ce.resolve_tz("local").utcoffset(datetime.now()))
        self.assertEqual(str(ce.resolve_tz("Europe/Berlin")), "Europe/Berlin")


if __name__ == "__main__":
    unittest.main()
