"""Tests for the shared pairing helpers (klok.pairing).

frame_at and is_practice were extracted here from dashboard.py so the
dashboard, the review ritual and the insights digest share one join. These
lock the join's exact behavior (half-open, latest-started wins, practice
classification) and a dashboard-payload invariant so the extraction can be
proven behavior-preserving.
"""

import unittest
from datetime import datetime, timedelta, timezone

from klok.dashboard import build_payload
from klok.mindful import BREATHING_TAG, Checkin
from klok.model import Frame
from klok.pairing import frame_at, is_practice

TZ = timezone.utc


def at(hour, minute=0):
    return datetime(2026, 9, 1, hour, minute, tzinfo=TZ)


class FrameAtTests(unittest.TestCase):
    def setUp(self):
        self.now = at(23)
        self.a = Frame(start=at(9), stop=at(11), project="acme")
        self.b = Frame(start=at(11), stop=at(12), project="internal")

    def test_moment_inside_a_frame_matches_it(self):
        self.assertIs(frame_at([self.a, self.b], at(10), self.now), self.a)

    def test_start_is_inclusive_end_is_exclusive(self):
        # half-open: start belongs to the block, the stop instant does not.
        self.assertIs(frame_at([self.a, self.b], at(9), self.now), self.a)
        self.assertIs(frame_at([self.a, self.b], at(11), self.now), self.b)

    def test_moment_outside_every_frame_is_none(self):
        self.assertIsNone(frame_at([self.a, self.b], at(8), self.now))
        self.assertIsNone(frame_at([self.a, self.b], at(12), self.now))

    def test_overlap_goes_to_the_latest_started(self):
        outer = Frame(start=at(9), stop=at(12), project="outer")
        inner = Frame(start=at(10), stop=at(11), project="inner")
        self.assertIs(frame_at([outer, inner], at(10, 30), self.now), inner)

    def test_running_frame_uses_now_for_its_end(self):
        running = Frame(start=at(9), stop=None, project="acme")
        self.assertIs(frame_at([running], at(15), self.now), running)
        self.assertIsNone(frame_at([running], at(23), at(23)))


class IsPracticeTests(unittest.TestCase):
    def test_practice_tag_marks_it(self):
        self.assertTrue(is_practice(Frame(start=at(9), tags=[BREATHING_TAG])))

    def test_practice_sheet_marks_it(self):
        frame = Frame(start=at(9), sheet="wellbeing")
        self.assertTrue(is_practice(frame, practice_sheet="wellbeing"))
        self.assertFalse(is_practice(frame, practice_sheet="default"))

    def test_plain_work_is_not_practice(self):
        self.assertFalse(is_practice(Frame(start=at(9), project="acme")))


class DashboardPayloadRegressionTests(unittest.TestCase):
    """Locks the dashboard's join output so moving frame_at cannot drift it."""

    def test_energy_mean_joins_checkins_to_their_frame(self):
        now = at(23)
        frames = [
            Frame(start=at(9), stop=at(11), project="acme"),
            Frame(start=at(11), stop=at(12), project="internal"),
        ]
        checkins = [
            Checkin(at=at(10), energy=4),      # inside acme
            Checkin(at=at(10, 30), energy=2),  # inside acme
            Checkin(at=at(11, 30), energy=5),  # inside internal
        ]
        payload = build_payload(frames, checkins, now)
        energy = {p["name"]: p["energy"] for p in payload["projects"]}
        self.assertEqual(energy["acme"], 3.0)       # (4 + 2) / 2
        self.assertEqual(energy["internal"], 5.0)
        # each check-in carries the project it joined to
        self.assertEqual([c["project"] for c in payload["checkins"]],
                         ["acme", "acme", "internal"])


if __name__ == "__main__":
    unittest.main()
