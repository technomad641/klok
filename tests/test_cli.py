import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from klok.cli import main


def scripted_input(answers):
    """A stand-in for the ``Context`` input seam that replays a script.

    Each prompt returns the next scripted answer; once they run out it returns
    ``None`` the way a real read does at EOF, so a `review` walk ends cleanly.
    """
    pending = iter(answers)

    def ask(prompt):
        try:
            return next(pending)
        except StopIteration:
            return None

    return ask


class CliTestCase(unittest.TestCase):
    """Drives the real entry point and reads what a user would see."""

    clock = "2026-08-31 15:00"

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="klok-cli-"))

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_klok(self, *args, clock=None, answers=None):
        argv = ["--home", str(self.home), "--no-color", "--now", clock or self.clock] + [str(a) for a in args]
        ask = scripted_input(answers) if answers is not None else None
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv, ask=ask)
        return code, out.getvalue() + err.getvalue()

    def assertOk(self, *args, **kwargs):
        code, output = self.run_klok(*args, **kwargs)
        self.assertEqual(code, 0, "expected success, got %d:\n%s" % (code, output))
        return output

    def run_klok_exit(self, *args, clock=None):
        """For argparse-level outcomes (--help, a bad subcommand): these call
        sys.exit() directly rather than returning a code, so they have to be
        caught as SystemExit instead of read as a return value."""
        argv = ["--home", str(self.home), "--no-color", "--now", clock or self.clock] + [str(a) for a in args]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                main(argv)
        return caught.exception.code, out.getvalue() + err.getvalue()

    def ids(self, *args):
        output = self.assertOk("log", ":all", "--all-sheets", "-f", "json", *args)
        return [row["id"] for row in json.loads(output)]


class TrackingTests(CliTestCase):
    def test_start_then_stop_records_the_interval(self):
        self.assertOk("start", "acme", "+api", "--at", "09:00")
        output = self.assertOk("stop", "--at", "10:30")
        self.assertIn("1h 30m", output)
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(rows[0]["project"], "acme")
        self.assertEqual(rows[0]["tags"], ["api"])
        self.assertEqual(rows[0]["seconds"], 5400)

    def test_status_reports_the_running_entry(self):
        self.assertOk("start", "acme", "--at", "14:00")
        output = self.assertOk("status")
        self.assertIn("acme", output)
        self.assertIn("1h", output)

    def test_status_is_an_error_when_idle(self):
        code, _ = self.run_klok("status")
        self.assertEqual(code, 1)

    def test_status_json(self):
        self.assertOk("start", "acme", "--at", "14:00")
        payload = json.loads(self.assertOk("status", "--json"))
        self.assertTrue(payload["running"])
        self.assertEqual(payload["elapsed_seconds"], 3600)

    def test_starting_again_stops_the_previous_entry(self):
        self.assertOk("start", "acme", "--at", "09:00")
        self.assertOk("start", "internal", "--at", "10:00")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["seconds"], 3600)
        self.assertTrue(rows[1]["running"])

    def test_no_stop_refuses_to_interrupt(self):
        self.assertOk("start", "acme", "--at", "09:00")
        code, output = self.run_klok("start", "internal", "--no-stop")
        self.assertEqual(code, 1)
        self.assertIn("already running", output)

    def test_relative_start_time(self):
        self.assertOk("start", "acme", "--at", "-30m")
        payload = json.loads(self.assertOk("status", "--json"))
        self.assertEqual(payload["elapsed_seconds"], 1800)

    def test_cancel_discards(self):
        self.assertOk("start", "acme", "--at", "09:00")
        self.assertOk("cancel")
        self.assertEqual(self.ids(), [])

    def test_stop_without_anything_running(self):
        code, output = self.run_klok("stop")
        self.assertEqual(code, 1)
        self.assertIn("Nothing is running", output)

    def test_track_records_a_past_interval(self):
        self.assertOk("track", "09:00", "to", "11:30", "acme", "+api")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(rows[0]["seconds"], 9000)

    def test_track_with_a_duration(self):
        self.assertOk("track", "--from", "09:00", "-d", "45m", "acme")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(rows[0]["seconds"], 2700)

    def test_track_with_a_dangling_separator_fails_cleanly(self):
        code, output = self.run_klok("track", "09:00", "-")
        self.assertEqual(code, 1)
        self.assertIn("klok:", output)

    def test_add_closes_the_open_stretch(self):
        self.assertOk("track", "09:00", "to", "10:00", "acme")
        self.assertOk("add", "internal", "+email", "--at", "10:30")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(rows[1]["project"], "internal")
        self.assertEqual(rows[1]["seconds"], 1800)

    def test_switch_hands_over_at_one_instant(self):
        self.assertOk("start", "acme", "--at", "09:00")
        self.assertOk("switch", "internal", "--at", "10:00")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(rows[0]["stop"], rows[1]["start"])

    def test_restart_reuses_project_and_tags(self):
        self.assertOk("track", "09:00", "to", "10:00", "acme", "+api")
        self.assertOk("restart", "--at", "11:00")
        payload = json.loads(self.assertOk("status", "--json"))
        self.assertEqual(payload["project"], "acme")
        self.assertEqual(payload["tags"], ["api"])

    def test_stretch_pulls_the_start_back(self):
        self.assertOk("track", "09:00", "to", "10:00", "acme")
        self.assertOk("track", "10:30", "to", "11:00", "internal")
        self.assertOk("stretch", "@1")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(rows[1]["seconds"], 3600)


class EditingTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.assertOk("track", "09:00", "to", "10:00", "acme", "+api", "-n", "first")
        self.assertOk("track", "11:00", "to", "12:00", "internal", "+meeting")

    def test_annotate(self):
        self.assertOk("annotate", "@1", "sprint", "planning")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(rows[1]["note"], "sprint planning")

    def test_annotate_without_an_id_uses_the_last_entry(self):
        self.assertOk("annotate", "sprint", "planning")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(rows[1]["note"], "sprint planning")

    def test_tag_add_and_remove(self):
        self.assertOk("tag", "@1", "+urgent", "-meeting")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(rows[1]["tags"], ["urgent"])

    def test_untag(self):
        self.assertOk("untag", "@1", "meeting")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(rows[1]["tags"], [])

    def test_rename_project(self):
        self.assertOk("rename", "project", "acme", "acme-corp")
        self.assertIn("acme-corp", self.assertOk("projects"))

    def test_rename_tag(self):
        self.assertOk("rename", "tag", "api", "backend")
        self.assertIn("backend", self.assertOk("tags"))

    def test_move_keeps_the_length(self):
        self.assertOk("move", "@2", "08:00")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertTrue(rows[0]["start"].endswith("08:00:00+00:00")
                        or "T08:00" in rows[0]["start"])
        self.assertEqual(rows[0]["seconds"], 3600)

    def test_lengthen_and_shorten(self):
        self.assertOk("lengthen", "@2", "30m")
        self.assertOk("shorten", "@2", "15m")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(rows[0]["seconds"], 4500)

    def test_shorten_past_zero_is_refused(self):
        code, output = self.run_klok("shorten", "@2", "2h")
        self.assertEqual(code, 1)
        self.assertIn("negative", output)

    def test_split_into_equal_pieces(self):
        self.assertOk("split", "@2", "--into", "2")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["seconds"], 1800)

    def test_split_at_a_time(self):
        self.assertOk("split", "@2", "--at", "09:15")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(rows[0]["seconds"], 900)

    def test_split_outside_the_entry_is_refused(self):
        code, output = self.run_klok("split", "@2", "--at", "14:00")
        self.assertEqual(code, 1)
        self.assertIn("outside", output)

    def test_join_merges_and_keeps_tags(self):
        ids = self.ids()
        self.assertOk("join", *ids)
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seconds"], 10800)
        self.assertEqual(sorted(rows[0]["tags"]), ["api", "meeting"])

    def test_fill_reaches_the_neighbour(self):
        self.assertOk("fill", "@1")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(rows[1]["seconds"], 5 * 3600)

    def test_edit_flags_change_fields(self):
        self.assertOk("edit", "@1", "--set-project", "other", "-n", "changed", "--tags", "x", "y")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual(rows[1]["project"], "other")
        self.assertEqual(rows[1]["tags"], ["x", "y"])
        self.assertEqual(rows[1]["note"], "changed")

    def test_delete_then_undo(self):
        self.assertOk("delete", "@1")
        self.assertEqual(len(self.ids()), 1)
        self.assertOk("undo")
        self.assertEqual(len(self.ids()), 2)

    def test_undo_with_nothing_to_undo(self):
        fresh = CliTestCase("run_klok")
        fresh.setUp()
        try:
            code, output = fresh.run_klok("undo")
            self.assertEqual(code, 1)
            self.assertIn("Nothing to undo", output)
        finally:
            fresh.tearDown()

    def test_unknown_id(self):
        code, output = self.run_klok("delete", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no entry matching", output)


class ReportingTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.assertOk("track", "09:00", "to", "10:30", "acme", "+api")
        self.assertOk("track", "10:30", "to", "11:00", "acme", "+docs")
        self.assertOk("track", "11:00", "to", "12:00", "internal", "+meeting")

    def test_log_groups_by_day_with_a_total(self):
        output = self.assertOk("log", ":day")
        self.assertIn("Mon 31 August 2026", output)
        self.assertIn("Total: 3h 00m", output)

    def test_report_totals_per_project(self):
        output = self.assertOk("report", ":day")
        self.assertIn("acme - 2h 00m", output)
        self.assertIn("internal - 1h 00m", output)

    def test_summary_has_a_daily_subtotal(self):
        output = self.assertOk("summary", ":day")
        self.assertIn("W36", output)
        self.assertIn("3h 00m", output)

    def test_project_filter(self):
        rows = json.loads(self.assertOk("log", ":day", "-p", "acme", "-f", "json"))
        self.assertEqual(len(rows), 2)

    def test_tag_filter(self):
        rows = json.loads(self.assertOk("log", ":day", "-t", "docs", "-f", "json"))
        self.assertEqual(len(rows), 1)

    def test_excluded_tag(self):
        rows = json.loads(self.assertOk("log", ":day", "-T", "meeting", "-f", "json"))
        self.assertEqual(len(rows), 2)

    def test_rounding_applies_to_reports(self):
        output = self.assertOk("--round", "60", "report", ":day")
        self.assertIn("Total: 4h 00m", output)

    def test_chart_modes_all_render(self):
        for mode in ("day", "project", "tag", "sheet", "punchcard"):
            output = self.assertOk("chart", ":day", "--by", mode)
            self.assertTrue(output.strip())

    def test_gaps(self):
        output = self.assertOk("gaps", ":day", "--min", "30m")
        self.assertIn("09:00", output)

    def test_stats(self):
        output = self.assertOk("stats", ":month")
        self.assertIn("Entries", output)
        self.assertIn("Top project", output)

    def test_from_and_to_with_clock_times_stay_inside_the_day(self):
        rows = json.loads(self.assertOk("log", "--from", "10:30", "--to", "11:00", "-f", "json"))
        self.assertEqual([row["project"] for row in rows], ["acme"])

    def test_to_with_a_bare_date_is_inclusive(self):
        rows = json.loads(self.assertOk("log", "--from", "2026-08-01", "--to", "2026-08-31",
                                        "-f", "json"))
        self.assertEqual(len(rows), 3)

    def test_empty_range_says_so(self):
        output = self.assertOk("log", ":yesterday")
        self.assertIn("No time tracked", output)

    def test_export_formats(self):
        self.assertIn("BEGIN:VCALENDAR", self.assertOk("export", ":day", "-f", "ical"))
        self.assertIn("i 2026/08/31", self.assertOk("export", ":day", "-f", "timeclock"))
        self.assertIn("| Date |", self.assertOk("export", ":day", "-f", "md"))
        self.assertIn("id,sheet,project", self.assertOk("export", ":day", "-f", "csv"))

    def test_export_import_round_trip(self):
        path = self.home / "dump.json"
        self.assertOk("export", ":all", "-f", "json", "-o", str(path))
        other = Path(tempfile.mkdtemp(prefix="klok-import-"))
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["--home", str(other), "--no-color", "--now", self.clock,
                             "import", str(path)])
            self.assertEqual(code, 0, out.getvalue() + err.getvalue())
            self.assertIn("Imported 3 entries", out.getvalue())
        finally:
            shutil.rmtree(other, ignore_errors=True)


class SheetAndConfigTests(CliTestCase):
    def test_sheets_isolate_entries(self):
        self.assertOk("track", "09:00", "to", "10:00", "acme")
        self.assertOk("sheet", "client")
        self.assertOk("track", "11:00", "to", "12:00", "bigcorp")
        self.assertIn("No time tracked", self.assertOk("log", ":day", "-p", "acme"))
        self.assertEqual(len(json.loads(self.assertOk("log", ":day", "--all-sheets", "-f", "json"))), 2)

    def test_sheet_dash_goes_back(self):
        self.assertOk("sheet", "client")
        self.assertIn("was client", self.assertOk("sheet", "-"))

    def test_sheets_listing(self):
        self.assertOk("track", "09:00", "to", "10:00", "acme")
        self.assertIn("default", self.assertOk("sheets"))

    def test_archive_hides_entries_from_the_default_sheet(self):
        self.assertOk("track", "09:00", "to", "10:00", "acme")
        self.assertOk("archive", ":day")
        self.assertIn("No time tracked", self.assertOk("log", ":day"))
        self.assertEqual(len(json.loads(self.assertOk("log", ":day", "--all-sheets", "-f", "json"))), 1)

    def test_config_set_and_get(self):
        self.assertOk("config", "set", "general.week_start", "sunday")
        self.assertEqual(self.assertOk("config", "get", "general.week_start").strip(), "sunday")
        self.assertIn("general.week_start = sunday", self.assertOk("config", "list"))

    def test_config_unset_falls_back_to_the_built_in_default(self):
        self.assertOk("config", "set", "general.default_project", "acme")
        self.assertOk("config", "unset", "general.default_project")
        self.assertEqual(self.assertOk("config", "get", "general.default_project").strip(), "")

    def test_config_get_on_an_unknown_key_fails(self):
        code, output = self.run_klok("config", "get", "general.nonsense")
        self.assertEqual(code, 1)
        self.assertIn("no such config key", output)

    def test_default_project_is_used(self):
        self.assertOk("config", "set", "general.default_project", "acme")
        self.assertOk("start", "--at", "09:00")
        self.assertEqual(json.loads(self.assertOk("status", "--json"))["project"], "acme")

    def test_check_finds_overlaps(self):
        self.assertOk("track", "09:00", "to", "11:00", "acme")
        self.assertOk("track", "10:00", "to", "12:00", "internal")
        code, output = self.run_klok("check")
        self.assertEqual(code, 1)
        self.assertIn("overlap", output)

    def test_check_is_happy_with_clean_data(self):
        self.assertOk("track", "09:00", "to", "10:00", "acme")
        self.assertIn("no problems", self.assertOk("check"))

    def test_where_lists_paths(self):
        self.assertIn("frames.jsonl", self.assertOk("where"))

    def test_completion_scripts(self):
        for shell in ("bash", "zsh", "fish"):
            self.assertIn("klok", self.assertOk("completion", shell))

    def test_version(self):
        self.assertIn("klok", self.assertOk("version"))

    def test_bare_invocation_shows_status(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([])
        self.assertIn(code, (0, 1))


class MindfulnessTests(CliTestCase):
    def test_pattern_listing(self):
        output = self.assertOk("breathe", "--list")
        self.assertIn("box", output)
        self.assertIn("4-7-8", output)

    def test_breathe_plan_default(self):
        output = self.assertOk("breathe", "--plan")
        self.assertIn("box (4-4-4-4)", output)
        self.assertIn("Rounds:   6", output)
        self.assertIn("Total: 1m 36s", output)

    def test_breathe_plan_for_a_duration_picks_the_round_count(self):
        output = self.assertOk("breathe", "4-7-8", "--for", "2m", "--plan")
        self.assertIn("Rounds:   6", output)
        self.assertNotIn("Rest", output)

    def test_breathe_plan_rejects_a_bad_pattern(self):
        code, output = self.run_klok("breathe", "sideways", "--plan")
        self.assertEqual(code, 1)
        self.assertIn("unknown pattern", output)

    def test_breathe_runs_and_can_skip_recording(self):
        self.assertOk("breathe", "0.02-0-0.02-0", "--rounds", "1", "--no-track", "-q")
        self.assertEqual(self.ids(), [])

    def test_meditate_plan_lists_the_bells(self):
        output = self.assertOk("meditate", "20m", "--interval-bell", "5m",
                               "--warmup", "30s", "--plan")
        self.assertIn("Sit:      20m", output)
        self.assertIn("5m", output)
        self.assertIn("sheet wellbeing", output)

    def test_meditate_plan_uses_the_configured_default(self):
        self.assertOk("config", "set", "mindful.default_sit", "7m")
        self.assertIn("Sit:      7m", self.assertOk("meditate", "--plan"))

    def test_meditate_rejects_a_zero_length_sit(self):
        code, output = self.run_klok("meditate", "0m", "--plan")
        self.assertEqual(code, 1)
        self.assertIn("positive", output)

    def test_practice_stays_off_the_working_sheet(self):
        self.assertOk("track", "09:00", "to", "10:00", "acme")
        self.assertOk("track", "07:00", "to", "07:20", "mindfulness", "+meditation",
                      "--sheet", "wellbeing")
        rows = json.loads(self.assertOk("log", ":day", "-f", "json"))
        self.assertEqual([row["project"] for row in rows], ["acme"])
        self.assertIn("mindfulness", self.assertOk("log", ":day", "--all-sheets"))

    def test_mindful_report(self):
        self.assertOk("track", "07:00", "to", "07:20", "mindfulness", "+meditation",
                      "--sheet", "wellbeing")
        self.assertOk("track", "12:00", "to", "12:05", "mindfulness", "+breathing",
                      "--sheet", "wellbeing")
        output = self.assertOk("mindful", ":day", "--no-chart")
        self.assertIn("Sessions         2", output)
        self.assertIn("Total practice   25m", output)
        self.assertIn("1 session, 20m", output)
        self.assertIn("Current streak   1 day", output)

    def test_mindful_with_no_practice(self):
        code, output = self.run_klok("mindful", ":day")
        self.assertEqual(code, 1)
        self.assertIn("No practice recorded", output)

    def test_mindful_ignores_ordinary_work_on_the_practice_sheet(self):
        self.assertOk("track", "07:00", "to", "08:00", "admin", "--sheet", "wellbeing")
        code, _ = self.run_klok("mindful", ":day")
        self.assertEqual(code, 1)

    def test_checkin_and_mood(self):
        self.assertOk("checkin", "--mood", "4", "--energy", "3", "--stress", "2",
                      "after", "standup")
        output = self.assertOk("mood", ":day")
        self.assertIn("after standup", output)
        self.assertIn("Mood", output)
        self.assertIn("avg 4.0", output)

    def test_checkin_needs_at_least_one_scale(self):
        code, output = self.run_klok("checkin", "just", "a", "note")
        self.assertEqual(code, 1)
        self.assertIn("at least one", output)

    def test_checkin_rejects_a_score_out_of_range(self):
        code, output = self.run_klok("checkin", "--mood", "9")
        self.assertEqual(code, 1)
        self.assertIn("between 1 and 5", output)

    def test_checkin_at_a_past_time(self):
        self.assertOk("checkin", "--mood", "3", "--at", "09:15")
        rows = json.loads(self.assertOk("mood", ":day", "--json"))
        self.assertTrue(rows[0]["at"].endswith("09:15:00+00:00") or "T09:15" in rows[0]["at"])

    def test_checkin_delete(self):
        self.assertOk("checkin", "--mood", "3")
        self.assertOk("checkin", "--delete", "@")
        code, _ = self.run_klok("mood", ":day")
        self.assertEqual(code, 1)

    def test_mood_with_no_check_ins(self):
        code, output = self.run_klok("mood", ":day")
        self.assertEqual(code, 1)
        self.assertIn("No check-ins", output)

    def test_mood_compares_busy_and_light_days(self):
        for day, mood, hours in ((25, 5, 2), (26, 5, 2), (27, 4, 3),
                                 (28, 2, 9), (29, 2, 9), (30, 3, 8)):
            clock = "2026-08-%d 20:00" % day
            self.assertOk("checkin", "--mood", str(mood), clock=clock)
            self.assertOk("track", "--from", "09:00", "-d", "%dh" % hours, "acme", clock=clock)
        output = self.assertOk("mood", ":month")
        self.assertIn("busier days", output)
        self.assertIn("lighter days", output)

    def test_status_suggests_a_pause_after_a_long_stretch(self):
        self.assertOk("start", "acme", "--at", "-3h")
        output = self.assertOk("status")
        self.assertIn("klok breathe", output)

    def test_status_stays_quiet_before_the_threshold(self):
        self.assertOk("start", "acme", "--at", "-10m")
        self.assertNotIn("klok breathe", self.assertOk("status"))

    def test_break_nudge_can_be_switched_off(self):
        self.assertOk("config", "set", "mindful.break_after", "")
        self.assertOk("start", "acme", "--at", "-3h")
        self.assertNotIn("klok breathe", self.assertOk("status"))


class CheckinShorthandTests(CliTestCase):
    """`klok checkin 4`, `klok stop --felt` and the flag forms coexisting."""

    def energies(self):
        """Every energy check-in, as (energy, HH:MM) pairs in time order."""
        path = self.home / "checkins.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if item.get("energy") is not None:
                out.append((item["energy"], item["at"][11:16]))
        return sorted(out, key=lambda pair: pair[1])

    def test_bare_integer_sets_energy(self):
        output = self.assertOk("checkin", "4")
        self.assertIn("energy 4", output)
        self.assertEqual([e for e, _ in self.energies()], [4])

    def test_flag_forms_still_work_alongside_the_positional(self):
        # The positional shorthand must not have broken --mood/--energy/--stress.
        self.assertOk("checkin", "--mood", "4")
        self.assertOk("checkin", "--energy", "2", "--mood", "3")
        self.assertEqual([e for e, _ in self.energies()], [2])

    def test_bare_integer_keeps_the_rest_as_a_note(self):
        self.assertOk("checkin", "4", "after", "lunch")
        output = self.assertOk("mood", ":day")
        self.assertIn("after lunch", output)

    def test_leading_dash_value_is_not_swallowed_as_the_positional(self):
        # `--energy 2 --mood 3` keeps parsing; words stays empty, no crash.
        self.assertOk("checkin", "--energy", "2", "--mood", "3")
        rows = json.loads(self.assertOk("mood", ":day", "--json"))
        self.assertEqual(rows[0]["energy"], 2)
        self.assertEqual(rows[0]["mood"], 3)

    def test_non_integer_first_word_is_a_note_not_a_rating(self):
        code, output = self.run_klok("checkin", "just", "a", "note")
        self.assertEqual(code, 1)
        self.assertIn("at least one", output)

    def test_stop_felt_records_an_energy_checkin_at_the_block_start(self):
        self.assertOk("start", "acme", "--at", "09:00")
        output = self.assertOk("stop", "--at", "10:30", "--felt", "4")
        self.assertIn("energy 4", output)
        self.assertEqual(self.energies(), [(4, "09:00")])

    def test_stop_felt_rejects_an_out_of_range_value_without_stopping(self):
        self.assertOk("start", "acme", "--at", "09:00")
        code, output = self.run_klok("stop", "--at", "10:30", "--felt", "9")
        self.assertEqual(code, 1)
        self.assertIn("between 1 and 5", output)
        # The frame was not stopped and no check-in was written.
        self.assertEqual(self.energies(), [])
        payload = json.loads(self.assertOk("status", "--json"))
        self.assertTrue(payload["running"])


class ReviewTests(CliTestCase):
    """The end-of-day `klok review` energy ritual."""

    def energies(self):
        path = self.home / "checkins.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            out.append(item)
        return sorted(out, key=lambda item: item["at"])

    def two_blocks(self):
        self.assertOk("track", "09:00", "to", "11:00", "acme", "+deep")
        self.assertOk("track", "11:00", "to", "11:20", "email")

    def test_happy_path_rates_each_block_at_its_start(self):
        self.two_blocks()
        output = self.assertOk("review", answers=["4", "2"])
        self.assertIn("acme +deep", output)
        self.assertIn("(09:00-11:00)", output)
        self.assertIn("Rated 2 blocks", output)
        energies = [(c["energy"], c["at"][11:16]) for c in self.energies()]
        self.assertEqual(energies, [(4, "09:00"), (2, "11:00")])

    def test_blank_line_skips_a_block(self):
        self.two_blocks()
        self.assertOk("review", answers=["", "3"])
        energies = [(c["energy"], c["at"][11:16]) for c in self.energies()]
        self.assertEqual(energies, [(3, "11:00")])

    def test_q_quits_the_walk(self):
        self.two_blocks()
        output = self.assertOk("review", answers=["4", "q"])
        self.assertIn("Rated 1 block.", output)
        energies = [c["energy"] for c in self.energies()]
        self.assertEqual(energies, [4])

    def test_back_re_rates_without_duplicating(self):
        self.two_blocks()
        # Rate block 0 as 3, step back from block 1, re-rate block 0 as 5, skip block 1.
        self.assertOk("review", answers=["3", "b", "5", ""])
        energies = [(c["energy"], c["at"][11:16]) for c in self.energies()]
        self.assertEqual(energies, [(5, "09:00")])

    def test_redo_replaces_an_existing_rating(self):
        self.two_blocks()
        self.assertOk("review", answers=["4", "2"])
        self.assertOk("review", "--redo", answers=["1", "5"])
        energies = [(c["energy"], c["at"][11:16]) for c in self.energies()]
        self.assertEqual(energies, [(1, "09:00"), (5, "11:00")])

    def test_already_rated_blocks_are_skipped_without_redo(self):
        self.two_blocks()
        self.assertOk("review", answers=["4"])  # rate only block 0
        # A second plain review only offers the still-unrated block.
        output = self.assertOk("review", answers=["3"])
        self.assertIn("email", output)
        self.assertNotIn("acme +deep", output)
        energies = [(c["energy"], c["at"][11:16]) for c in self.energies()]
        self.assertEqual(energies, [(4, "09:00"), (3, "11:00")])

    def test_mood_only_checkin_does_not_mask_an_unrated_block(self):
        self.assertOk("track", "09:00", "to", "11:00", "acme")
        # A mood-only check-in inside the block has energy None.
        self.assertOk("checkin", "--mood", "4", "--at", "10:00")
        output = self.assertOk("review", answers=["3"])
        self.assertIn("acme", output)
        self.assertIn("Rated 1 block", output)
        energies = [(c["energy"], c["at"][11:16]) for c in self.energies()
                    if c["energy"] is not None]
        self.assertEqual(energies, [(3, "09:00")])

    def test_practice_and_running_frames_are_left_out(self):
        self.assertOk("track", "09:00", "to", "10:00", "acme")
        # A practice session (meditation tag) on the default work sheet.
        self.assertOk("track", "10:00", "to", "10:20", "mindfulness", "+meditation")
        # A still-running frame.
        self.assertOk("start", "later", "--at", "12:00")
        output = self.assertOk("review", answers=["4", "4", "4"])
        self.assertIn("acme", output)
        self.assertNotIn("mindfulness", output)
        self.assertNotIn("later", output)
        self.assertIn("Rated 1 block", output)
        energies = [(c["energy"], c["at"][11:16]) for c in self.energies()]
        self.assertEqual(energies, [(4, "09:00")])

    def test_default_scope_is_the_active_sheet_only(self):
        self.assertOk("track", "09:00", "to", "10:00", "acme")
        self.assertOk("track", "10:00", "to", "11:00", "sideproj", "--sheet", "personal")
        # Plain review walks the active sheet (default) only.
        output = self.assertOk("review", answers=["4"])
        self.assertIn("acme", output)
        self.assertNotIn("sideproj", output)
        self.assertEqual([c["energy"] for c in self.energies()], [4])

    def test_all_sheets_widens_the_scope(self):
        self.assertOk("track", "09:00", "to", "10:00", "acme")
        self.assertOk("track", "10:00", "to", "11:00", "sideproj", "--sheet", "personal")
        output = self.assertOk("review", "--all-sheets", answers=["4", "5"])
        self.assertIn("acme", output)
        self.assertIn("sideproj", output)
        energies = [(c["energy"], c["at"][11:16]) for c in self.energies()]
        self.assertEqual(energies, [(4, "09:00"), (5, "10:00")])

    def test_invalid_answer_reprompts_the_same_block(self):
        self.two_blocks()
        # "9" is out of range: re-prompt the same block, then accept 4.
        output = self.assertOk("review", answers=["9", "4", "2"])
        self.assertIn("between 1 and 5", output)
        energies = [(c["energy"], c["at"][11:16]) for c in self.energies()]
        self.assertEqual(energies, [(4, "09:00"), (2, "11:00")])

    def test_nothing_to_review_is_a_clean_no_op(self):
        output = self.assertOk("review")
        self.assertIn("No blocks to review", output)


class DashboardTests(CliTestCase):
    def payload(self, *args):
        out = self.home / "dash.html"
        self.assertOk("dashboard", "--no-open", "-o", out, *args)
        page = out.read_text(encoding="utf-8")
        marker = "const KLOK = "
        start = page.index(marker) + len(marker)
        return json.loads(page[start:page.index(";\n", start)])

    def test_check_ins_are_joined_to_the_frame_running_at_the_time(self):
        self.assertOk("track", "09:00", "to", "11:00", "acme", "+deep")
        self.assertOk("track", "11:00", "to", "11:20", "email")
        self.assertOk("checkin", "--energy", "4", "--mood", "5", "--at", "10:00")
        self.assertOk("checkin", "--energy", "2", "--at", "11:10")
        self.assertOk("checkin", "--mood", "3", "--at", "13:00")
        data = self.payload()
        self.assertEqual([c["project"] for c in data["checkins"]], ["acme", "email", None])
        self.assertEqual([p["name"] for p in data["projects"]], ["acme", "email"])
        self.assertEqual(data["projects"][0]["energy"], 4)

    def test_deep_work_follows_tags_and_session_length(self):
        self.assertOk("track", "09:00", "to", "11:00", "acme", "+deep")
        self.assertOk("track", "11:00", "to", "11:10", "acme", "+deep")
        self.assertOk("track", "12:00", "to", "14:00", "email")
        data = self.payload()
        self.assertEqual([f["deep"] for f in data["frames"]], [True, False, False])
        self.assertIn("+deep", data["deep"]["basis"])

    def test_without_deep_tags_every_long_session_counts(self):
        self.assertOk("track", "09:00", "to", "11:00", "acme")
        self.assertOk("track", "11:00", "to", "11:10", "email")
        data = self.payload()
        self.assertEqual([f["deep"] for f in data["frames"]], [True, False])

    def test_practice_sessions_are_marked_and_never_deep(self):
        self.assertOk("track", "07:00", "to", "07:30", "mindfulness", "+meditation",
                      "--sheet", "wellbeing")
        data = self.payload()
        self.assertTrue(data["projects"][0]["practice"])
        self.assertFalse(data["frames"][0]["deep"])

    def test_range_limits_what_is_included(self):
        self.assertOk("track", "2026-08-01 09:00", "to", "2026-08-01 10:00", "old")
        self.assertOk("track", "09:00", "to", "10:00", "acme")
        data = self.payload(":day")
        self.assertEqual([f["project"] for f in data["frames"]], ["acme"])

    def test_project_names_cannot_break_out_of_the_script(self):
        self.assertOk("track", "09:00", "to", "10:00", "</script><b>x", "--force")
        page = (self.home / "dash.html")
        self.assertOk("dashboard", "--no-open", "-o", page)
        self.assertNotIn("</script><b>", page.read_text(encoding="utf-8"))

    def test_empty_range_explains_what_to_do(self):
        code, output = self.run_klok("dashboard", "--no-open")
        self.assertEqual(code, 1)
        self.assertIn("--demo", output)

    def test_demo_needs_no_data(self):
        output = self.assertOk("dashboard", "--demo", "--no-open")
        self.assertIn("demo data", output)
        page = (self.home / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("/*KLOK_DATA*/null", page)


class HelpAndErrorsTests(CliTestCase):
    def test_top_level_help_is_grouped_with_a_quick_start(self):
        code, output = self.run_klok_exit("--help")
        self.assertEqual(code, 0)
        self.assertIn("Quick start:", output)
        self.assertIn("klok breathe", output)
        for heading in ("Tracking:", "Fixing entries:", "Reporting:",
                        "Sheets & config:", "Focus & mindfulness:", "Other:"):
            self.assertIn(heading, output)

    def test_top_level_help_does_not_dump_a_wall_of_choices(self):
        # The old argparse default listed every command and alias inline;
        # the grouped rundown replaces that, one command per line instead.
        code, output = self.run_klok_exit("--help")
        self.assertEqual(code, 0)
        self.assertNotIn("choose from", output)

    def test_a_single_commands_help_still_works(self):
        code, output = self.run_klok_exit("start", "--help")
        self.assertEqual(code, 0)
        self.assertIn("Start tracking a project", output)

    def test_typo_suggests_the_close_command(self):
        code, output = self.run_klok_exit("statuss")
        self.assertEqual(code, 2)
        self.assertIn("did you mean 'status'", output)

    def test_unrelated_word_gets_a_short_error_not_a_wall_of_choices(self):
        code, output = self.run_klok_exit("frobnicate")
        self.assertEqual(code, 2)
        self.assertIn("unknown command 'frobnicate'", output)
        self.assertNotIn("choose from", output)

    def test_an_alias_typo_is_still_recognised_as_a_real_command(self):
        # "zen" is an alias of meditate, not a typo needing a suggestion.
        code, output = self.run_klok_exit("zen", "--help")
        self.assertEqual(code, 0)

    def test_cold_status_offers_a_starter_hint(self):
        code, output = self.run_klok("status")
        self.assertEqual(code, 1)
        self.assertIn("Nothing tracked yet.", output)
        self.assertIn("klok start acme +api", output)

    def test_status_after_some_history_is_terser(self):
        self.assertOk("track", "09:00", "to", "10:00", "acme")
        code, output = self.run_klok("status")
        self.assertEqual(code, 1)
        self.assertIn("Nothing running.", output)
        self.assertNotIn("Nothing tracked yet", output)


class DigestTests(CliTestCase):
    """`klok digest`: project/tag drain-vs-lift from the energy ratings."""

    def ten_rated_blocks(self):
        # Five draining acme blocks, five lifting writing blocks, same day.
        for hour in range(1, 6):
            self.assertOk("track", "0%d:00" % hour, "to", "0%d:45" % hour, "acme", "+reviews")
        for hour in range(1, 6):
            self.assertOk("track", "1%d:00" % hour, "to", "1%d:45" % hour, "writing", "+deep")
        # review walks in start order: the five acme blocks, then the five writing blocks.
        self.assertOk("review", answers=["2"] * 5 + ["5"] * 5)

    def test_digest_surfaces_drain_and_lift(self):
        self.ten_rated_blocks()
        output = self.assertOk("digest")
        self.assertIn("acme drains you", output)
        self.assertIn("writing lifts you", output)
        self.assertIn("+reviews drains you", output)
        self.assertIn("Based on 10 rated blocks across 1 day.", output)

    def test_digest_with_no_ratings_says_so(self):
        output = self.assertOk("digest")
        self.assertIn("Nothing rated yet", output)
        self.assertIn("Based on 0 rated blocks across 0 days.", output)

    def test_too_few_blocks_report_no_pattern(self):
        self.assertOk("track", "09:00", "to", "10:00", "acme")
        self.assertOk("review", answers=["5"])
        output = self.assertOk("digest")
        self.assertIn("No clear pattern yet", output)
        self.assertIn("Based on 1 rated block across 1 day.", output)

    def test_digest_writes_a_plain_file(self):
        self.ten_rated_blocks()
        path = self.home / "digest.txt"
        self.assertOk("digest", "-o", str(path))
        text = path.read_text(encoding="utf-8")
        self.assertIn("drains you", text)
        self.assertIn("Based on 10 rated blocks", text)
        self.assertNotIn("\x1b[", text)  # no terminal colour codes in the shared copy


if __name__ == "__main__":
    unittest.main()
