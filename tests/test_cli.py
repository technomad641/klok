import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from klok.cli import main


class CliTestCase(unittest.TestCase):
    """Drives the real entry point and reads what a user would see."""

    clock = "2026-08-31 15:00"

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="klok-cli-"))

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_klok(self, *args, clock=None):
        argv = ["--home", str(self.home), "--no-color", "--now", clock or self.clock] + [str(a) for a in args]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue() + err.getvalue()

    def assertOk(self, *args, **kwargs):
        code, output = self.run_klok(*args, **kwargs)
        self.assertEqual(code, 0, "expected success, got %d:\n%s" % (code, output))
        return output

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


if __name__ == "__main__":
    unittest.main()
