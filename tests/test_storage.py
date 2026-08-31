import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from klok.config import Config
from klok.formats import parse_import, to_csv, to_json, to_timeclock
from klok.model import Frame
from klok.storage import Store, StoreError

NOW = datetime(2026, 8, 31, 14, 0).astimezone()


def frame(hour, length_minutes=60, project="acme", tags=("api",), sheet="default", **kwargs):
    start = NOW.replace(hour=hour, minute=0, second=0, microsecond=0)
    stop = None if length_minutes is None else start + timedelta(minutes=length_minutes)
    return Frame(start=start, stop=stop, project=project, tags=list(tags), sheet=sheet, **kwargs)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="klok-test-"))
        self.config = Config(self.home)
        self.store = Store(self.config)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def fresh_store(self):
        return Store(Config(self.home))


class RoundTripTests(StoreTestCase):
    def test_save_and_reload(self):
        self.store.add(frame(9))
        self.store.add(frame(11, project="internal", tags=["meeting"]))
        self.store.save("test")
        reloaded = self.fresh_store()
        self.assertEqual(len(reloaded.frames), 2)
        self.assertEqual(reloaded.frames[0].project, "acme")
        self.assertEqual(reloaded.frames[1].tags, ["meeting"])

    def test_frames_stay_sorted_by_start(self):
        self.store.add(frame(15))
        self.store.add(frame(9))
        self.store.save()
        self.assertEqual([item.start.hour for item in self.fresh_store().frames], [9, 15])

    def test_file_is_one_json_object_per_line(self):
        self.store.add(frame(9))
        self.store.save()
        lines = self.config.frames_path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["project"], "acme")

    def test_corrupt_line_is_reported_with_its_number(self):
        self.config.ensure_home()
        self.config.frames_path.write_text('{"start": "2026-08-31T09:00:00"}\nnot json\n')
        with self.assertRaises(StoreError) as caught:
            self.fresh_store().frames
        self.assertIn("line 2", str(caught.exception))


class LookupTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.first = self.store.add(frame(9, id="aaaa1111"))
        self.second = self.store.add(frame(11, id="bbbb2222", project="internal"))
        self.running = self.store.add(frame(13, length_minutes=None, id="cccc3333"))
        self.store.save()

    def test_by_full_and_partial_id(self):
        self.assertIs(self.store.by_id("aaaa1111"), self.first)
        self.assertIs(self.store.by_id("bbbb"), self.second)

    def test_by_index(self):
        self.assertIs(self.store.by_id("@1"), self.running)
        self.assertIs(self.store.by_id("@2"), self.second)

    def test_at_symbol_prefers_the_running_entry(self):
        self.assertIs(self.store.by_id("@"), self.running)

    def test_unknown_reference(self):
        with self.assertRaises(StoreError):
            self.store.by_id("zzzz")

    def test_running_and_last(self):
        self.assertIs(self.store.running(), self.running)
        self.assertIs(self.store.last(), self.running)

    def test_select_by_project_and_tag(self):
        self.assertEqual(len(self.store.select(projects=["acme"], now=NOW)), 2)
        self.assertEqual(len(self.store.select(tags=["api"], now=NOW)), 3)
        self.assertEqual(len(self.store.select(exclude_tags=["api"], now=NOW)), 0)

    def test_select_by_range(self):
        start = NOW.replace(hour=10, minute=0, second=0, microsecond=0)
        end = NOW.replace(hour=12, minute=0, second=0, microsecond=0)
        self.assertEqual([f.id for f in self.store.select(start, end, now=NOW)], ["bbbb2222"])

    def test_project_filter_matches_parents_and_globs(self):
        self.store.add(frame(16, project="acme.api", id="dddd4444"))
        self.assertEqual(len(self.store.select(projects=["acme"], now=NOW)), 3)
        self.assertEqual(len(self.store.select(projects=["ac*"], now=NOW)), 3)


class UndoTests(StoreTestCase):
    def test_undo_restores_the_previous_file(self):
        self.store.add(frame(9))
        self.store.save("first")
        self.store.add(frame(11))
        self.store.save("second")
        self.assertEqual(len(self.fresh_store().frames), 2)
        self.assertEqual(self.store.undo(), "second")
        self.assertEqual(len(self.fresh_store().frames), 1)

    def test_undo_on_an_empty_history(self):
        self.assertIsNone(self.store.undo())

    def test_undo_can_be_repeated(self):
        for hour in (9, 11, 13):
            self.store.add(frame(hour))
            self.store.save("add %d" % hour)
        self.store.undo()
        self.store.undo()
        self.assertEqual(len(self.fresh_store().frames), 1)


class AnalysisTests(StoreTestCase):
    def test_overlaps_are_found(self):
        self.store.add(frame(9, 120, id="aaaa1111"))
        self.store.add(frame(10, 60, id="bbbb2222"))
        self.store.add(frame(13, 60, id="cccc3333"))
        found = self.store.overlaps(NOW)
        self.assertEqual(len(found), 1)
        self.assertEqual({found[0][0].id, found[0][1].id}, {"aaaa1111", "bbbb2222"})

    def test_overlaps_ignore_other_sheets(self):
        self.store.add(frame(9, 120, sheet="a"))
        self.store.add(frame(10, 60, sheet="b"))
        self.assertEqual(self.store.overlaps(NOW), [])

    def test_gaps(self):
        self.store.add(frame(9, 60))
        self.store.add(frame(11, 60))
        start = NOW.replace(hour=9, minute=0, second=0, microsecond=0)
        end = NOW.replace(hour=12, minute=0, second=0, microsecond=0)
        gaps = self.store.gaps(start, end, now=NOW)
        self.assertEqual(len(gaps), 1)
        self.assertEqual((gaps[0][0].hour, gaps[0][1].hour), (10, 11))

    def test_sheets_and_switching(self):
        self.store.add(frame(9, sheet="client"))
        self.store.save()
        self.assertEqual(self.store.current_sheet, "default")
        self.store.set_sheet("client")
        self.assertEqual(self.fresh_store().current_sheet, "client")
        self.assertEqual(self.fresh_store().set_sheet("-"), "default")


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.frames = [frame(9, 90, tags=["api"], id="aaaa1111"),
                       frame(11, 30, project="internal", tags=["meeting"], id="bbbb2222")]

    def test_json_round_trip(self):
        restored = parse_import(to_json(self.frames, NOW))
        self.assertEqual([item.id for item in restored], ["aaaa1111", "bbbb2222"])
        self.assertEqual(restored[0].start, self.frames[0].start)

    def test_csv_round_trip(self):
        restored = parse_import(to_csv(self.frames, NOW))
        self.assertEqual(restored[1].project, "internal")
        self.assertEqual(restored[0].tags, ["api"])

    def test_timeclock_shape(self):
        lines = to_timeclock(self.frames, NOW).splitlines()
        self.assertTrue(lines[0].startswith("i 2026/08/31 09:00:00 acme:api"))
        self.assertTrue(lines[1].startswith("o 2026/08/31 10:30:00"))

    def test_running_frame_exports_with_an_empty_stop(self):
        rows = json.loads(to_json([frame(9, None)], NOW))
        self.assertEqual(rows[0]["stop"], "")
        self.assertTrue(rows[0]["running"])


if __name__ == "__main__":
    unittest.main()
