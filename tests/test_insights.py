"""Tests for the project/tag drain analyzer (klok.insights).

The analyzer joins energy-bearing check-ins to the frame each fell inside,
means them per block, and reports a project or tag only when it clears the
suppression rule (``n >= MIN_BLOCKS`` and ``|effect| >= MIN_EFFECT``).  These
build frames and check-ins directly and lock the exact Finding values.
"""

import unittest
from datetime import datetime, timedelta, timezone

from klok.insights import MIN_BLOCKS, MIN_EFFECT, Finding, drain_findings
from klok.mindful import Checkin
from klok.model import Frame

TZ = timezone.utc
BASE = datetime(2026, 9, 1, tzinfo=TZ)
NOW = BASE + timedelta(days=2)


def block(index, project="", tags=(), energies=(3,)):
    """One 30-minute frame plus its check-ins, stamped at the block start."""
    start = BASE + timedelta(hours=index)
    frame = Frame(start=start, stop=start + timedelta(minutes=30),
                  project=project, tags=list(tags))
    checks = [Checkin(at=start, energy=energy) for energy in energies]
    return frame, checks


def scenario(*blocks):
    frames, checkins = [], []
    for frame, checks in blocks:
        frames.append(frame)
        checkins.extend(checks)
    return frames, checkins


class ConstantsTests(unittest.TestCase):
    def test_thresholds_are_the_agreed_defaults(self):
        self.assertEqual(MIN_BLOCKS, 5)
        self.assertEqual(MIN_EFFECT, 0.5)


class SuppressionTests(unittest.TestCase):
    def test_no_checkins_means_no_findings(self):
        frames = [Frame(start=BASE, stop=BASE + timedelta(hours=1), project="acme")]
        self.assertEqual(drain_findings(frames, [], NOW), [])

    def test_checkin_outside_every_frame_is_ignored(self):
        frame = Frame(start=BASE, stop=BASE + timedelta(hours=1), project="acme")
        stray = Checkin(at=BASE + timedelta(hours=5), energy=1)
        self.assertEqual(drain_findings([frame], [stray], NOW), [])

    def test_single_rated_block_is_suppressed(self):
        # one rated block: n = 1, well under MIN_BLOCKS
        frames, checkins = scenario(block(0, project="acme", energies=(1,)))
        self.assertEqual(drain_findings(frames, checkins, NOW), [])

    def test_all_equal_energy_is_suppressed(self):
        # six rated blocks, every one energy 3: the effect is exactly 0
        frames, checkins = scenario(*[block(i, project="acme", energies=(3,))
                                      for i in range(6)])
        self.assertEqual(drain_findings(frames, checkins, NOW), [])


class ProjectFindingTests(unittest.TestCase):
    def test_clear_drain_and_lift_surface_ranked(self):
        blocks = []
        # five draining acme blocks; each block averages (1 + 3) / 2 = 2.0
        for i in range(5):
            blocks.append(block(i, project="acme", energies=(1, 3)))
        # five lifting writing blocks at energy 4
        for i in range(5, 10):
            blocks.append(block(i, project="writing", energies=(4,)))
        frames, checkins = scenario(*blocks)

        findings = drain_findings(frames, checkins, NOW)

        # overall = (5 * 2.0 + 5 * 4.0) / 10 = 3.0; ranked most draining first
        self.assertEqual(len(findings), 2)
        drain, lift = findings
        self.assertEqual(drain, Finding(
            headline="acme drains you",
            detail="averages 1.0 points below your overall energy over 5 blocks",
            effect_size=-1.0, n=5))
        self.assertEqual(lift, Finding(
            headline="writing lifts you",
            detail="averages 1.0 points above your overall energy over 5 blocks",
            effect_size=1.0, n=5))


class TagFindingTests(unittest.TestCase):
    def test_tag_findings_surface_while_the_uniform_project_is_suppressed(self):
        blocks = []
        for i in range(5):
            blocks.append(block(i, project="work", tags=("reviews",), energies=(2,)))
        for i in range(5, 10):
            blocks.append(block(i, project="work", tags=("deep",), energies=(4,)))
        frames, checkins = scenario(*blocks)

        findings = drain_findings(frames, checkins, NOW)

        # project "work" covers all ten blocks: mean 3.0 == overall, effect 0
        headlines = [finding.headline for finding in findings]
        self.assertNotIn("work drains you", headlines)
        self.assertNotIn("work lifts you", headlines)
        self.assertEqual(len(findings), 2)
        drain, lift = findings
        self.assertEqual(drain, Finding(
            headline="+reviews drains you",
            detail="averages 1.0 points below your overall energy over 5 blocks",
            effect_size=-1.0, n=5))
        self.assertEqual(lift, Finding(
            headline="+deep lifts you",
            detail="averages 1.0 points above your overall energy over 5 blocks",
            effect_size=1.0, n=5))


class MoodOnlyTests(unittest.TestCase):
    def test_mood_only_checkin_creates_no_block(self):
        frame = Frame(start=BASE, stop=BASE + timedelta(hours=1), project="acme")
        mood = Checkin(at=BASE + timedelta(minutes=10), mood=4)  # energy is None
        self.assertEqual(drain_findings([frame], [mood], NOW), [])

    def test_mood_only_checkin_does_not_inflate_a_rated_project(self):
        blocks = [block(i, project="acme", energies=(2,)) for i in range(5)]
        blocks += [block(i, project="writing", energies=(4,)) for i in range(5, 10)]
        frames, checkins = scenario(*blocks)
        # a sixth acme frame whose only check-in is mood-only (energy None)
        extra_start = BASE + timedelta(hours=20)
        frames.append(Frame(start=extra_start, stop=extra_start + timedelta(minutes=30),
                            project="acme"))
        checkins.append(Checkin(at=extra_start, mood=5))

        findings = drain_findings(frames, checkins, NOW)

        acme = next(finding for finding in findings
                    if finding.headline == "acme drains you")
        self.assertEqual(acme.n, 5)            # the mood-only block did not count
        self.assertEqual(acme.effect_size, -1.0)


if __name__ == "__main__":
    unittest.main()
