import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from klok.config import Config
from klok.mindful import (Checkin, CheckinStore, MindfulError, breath_plan,
                          breathe, break_due, check_score, cycle_length,
                          daily_averages, describe_pattern, longest_streak,
                          parse_pattern, pattern_phases, rounds_for, sit_plan,
                          sparkline, streak)
from klok.model import Frame
from klok.utils import Term

NOW = datetime(2026, 8, 31, 14, 0).astimezone()


class FakeClock:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += max(seconds, 0.001)


class PatternTests(unittest.TestCase):
    def test_named_patterns(self):
        self.assertEqual(parse_pattern("box")[:4], (4, 4, 4, 4))
        self.assertEqual(parse_pattern("relax")[:4], (4, 7, 8, 0))
        self.assertEqual(parse_pattern("coherent")[:4], (5.5, 0, 5.5, 0))

    def test_pattern_defaults_to_box(self):
        self.assertEqual(parse_pattern(None)[4], "box")

    def test_three_counts_mean_no_pause_at_the_bottom(self):
        self.assertEqual(parse_pattern("4-7-8")[:4], (4, 7, 8, 0))

    def test_two_counts_are_in_and_out(self):
        self.assertEqual(parse_pattern("4-6")[:4], (4, 0, 6, 0))

    def test_four_counts_are_taken_as_given(self):
        self.assertEqual(parse_pattern("5-2-7-2")[:4], (5, 2, 7, 2))

    def test_bad_patterns(self):
        for text in ("nope", "4-7-8-9-10", "a-b", "0-0-0-0", "-4--7"):
            with self.assertRaises(MindfulError, msg=text):
                parse_pattern(text)

    def test_zero_phases_are_skipped(self):
        phases = pattern_phases(parse_pattern("4-6"))
        self.assertEqual([key for key, _, _ in phases], ["inhale", "exhale"])

    def test_cycle_length_and_round_count(self):
        box = parse_pattern("box")
        self.assertEqual(cycle_length(box), timedelta(seconds=16))
        self.assertEqual(rounds_for(box, timedelta(minutes=2)), 8)
        self.assertEqual(rounds_for(box, timedelta(seconds=1)), 1)

    def test_plan_covers_every_phase_of_every_round(self):
        plan = breath_plan(parse_pattern("4-7-8"), 3)
        self.assertEqual(len(plan), 9)
        self.assertEqual(plan[0][1], "inhale")
        self.assertEqual(plan[-1][0], 3)

    def test_description_mentions_a_named_pattern(self):
        self.assertIn("box (4-4-4-4)", describe_pattern(parse_pattern("box")))
        self.assertEqual(describe_pattern(parse_pattern("4-7-8")), "4-7-8-0")


class PacerTests(unittest.TestCase):
    def test_breathe_walks_the_whole_plan_without_real_time(self):
        clock = FakeClock()
        finished = breathe(parse_pattern("box"), 2, Term("never"), quiet=True,
                           bell=False, notify_on_end=False,
                           sleep=clock.sleep, clock=clock)
        self.assertTrue(finished)
        self.assertGreaterEqual(clock.value, 32)

    def test_sit_plan_has_warmup_intervals_and_a_closing_bell(self):
        marks = sit_plan(timedelta(minutes=20), timedelta(minutes=5), timedelta(seconds=30))
        self.assertEqual(marks, [30.0, 300.0, 600.0, 900.0, 1200.0])

    def test_sit_plan_without_intervals_is_just_the_end(self):
        self.assertEqual(sit_plan(timedelta(minutes=10), None), [600.0])

    def test_interval_longer_than_the_sit_adds_nothing(self):
        self.assertEqual(sit_plan(timedelta(minutes=5), timedelta(minutes=30)), [300.0])


class ScoreTests(unittest.TestCase):
    def test_valid_scores(self):
        self.assertEqual(check_score("mood", "4"), 4)
        self.assertIsNone(check_score("mood", None))

    def test_out_of_range(self):
        for value in (0, 6, -1):
            with self.assertRaises(MindfulError):
                check_score("mood", value)

    def test_not_a_number(self):
        with self.assertRaises(MindfulError):
            check_score("energy", "great")


class CheckinStoreTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="klok-mind-"))
        self.config = Config(self.home)
        self.store = CheckinStore(self.config)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def make(self, day, mood=3, **kwargs):
        return Checkin(at=NOW.replace(day=day, hour=9, minute=0), mood=mood, **kwargs)

    def test_round_trip(self):
        self.store.add(self.make(30, mood=4, energy=2, note="tired", tags=["am"]))
        self.store.save()
        reloaded = CheckinStore(Config(self.home)).items
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0].mood, 4)
        self.assertEqual(reloaded[0].energy, 2)
        self.assertEqual(reloaded[0].note, "tired")
        self.assertEqual(reloaded[0].tags, ["am"])

    def test_items_are_sorted_and_selectable_by_range(self):
        self.store.add(self.make(30))
        self.store.add(self.make(28))
        self.store.save()
        self.assertEqual([item.at.day for item in self.store.items], [28, 30])
        window = self.store.select(NOW.replace(day=29, hour=0, minute=0), NOW)
        self.assertEqual([item.at.day for item in window], [30])

    def test_remove_by_prefix_and_by_at(self):
        first = self.store.add(self.make(28))
        self.store.add(self.make(30))
        self.store.save()
        self.assertEqual(self.store.remove(first.id[:4]).id, first.id)
        self.assertEqual(self.store.remove("@").at.day, 30)
        self.store.save()
        self.assertEqual(CheckinStore(Config(self.home)).items, [])

    def test_removing_something_that_is_not_there(self):
        with self.assertRaises(MindfulError):
            self.store.remove("zzzz")

    def test_corrupt_line_is_reported(self):
        self.config.ensure_home()
        self.config.checkins_path.write_text('{"at": "2026-08-30T09:00:00"}\nrubbish\n')
        with self.assertRaises(MindfulError) as caught:
            CheckinStore(Config(self.home)).items
        self.assertIn("line 2", str(caught.exception))


class TrendTests(unittest.TestCase):
    def test_daily_averages_skip_unanswered_scales(self):
        checkins = [Checkin(at=NOW.replace(day=30, hour=9), mood=4, energy=None),
                    Checkin(at=NOW.replace(day=30, hour=17), mood=2, energy=3),
                    Checkin(at=NOW.replace(day=31, hour=9), mood=5, energy=None)]
        self.assertEqual(daily_averages(checkins, "mood"),
                         {date(2026, 8, 30): 3.0, date(2026, 8, 31): 5.0})
        self.assertEqual(daily_averages(checkins, "energy"), {date(2026, 8, 30): 3.0})

    def test_sparkline_spans_the_block_characters(self):
        line = sparkline([1, 3, 5])
        self.assertEqual(len(line), 3)
        self.assertEqual(line[0], "▁")
        self.assertEqual(line[-1], "█")

    def test_sparkline_leaves_a_space_for_missing_days(self):
        self.assertEqual(sparkline([1, None, 5])[1], " ")

    def test_sparkline_of_nothing(self):
        self.assertEqual(sparkline([None, None]), "")

    def test_streak_counts_back_from_today(self):
        today = date(2026, 8, 31)
        days = {today, today - timedelta(days=1), today - timedelta(days=2)}
        self.assertEqual(streak(days, today), 3)

    def test_streak_tolerates_not_having_practised_yet_today(self):
        today = date(2026, 8, 31)
        self.assertEqual(streak({today - timedelta(days=1)}, today), 1)

    def test_streak_is_broken_by_a_two_day_gap(self):
        today = date(2026, 8, 31)
        self.assertEqual(streak({today - timedelta(days=2)}, today), 0)

    def test_longest_streak(self):
        days = {date(2026, 8, d) for d in (1, 2, 3, 10, 11)}
        self.assertEqual(longest_streak(days), 3)
        self.assertEqual(longest_streak(set()), 0)


class BreakNudgeTests(unittest.TestCase):
    def frame(self, minutes):
        return Frame(start=NOW - timedelta(minutes=minutes), stop=None, project="acme")

    def test_due_after_the_threshold(self):
        self.assertTrue(break_due(self.frame(120), timedelta(minutes=90), NOW))

    def test_not_due_before_it(self):
        self.assertFalse(break_due(self.frame(30), timedelta(minutes=90), NOW))

    def test_never_due_with_nothing_running_or_no_threshold(self):
        self.assertFalse(break_due(None, timedelta(minutes=90), NOW))
        self.assertFalse(break_due(self.frame(600), timedelta(0), NOW))


if __name__ == "__main__":
    unittest.main()
