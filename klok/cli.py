"""Command line entry point."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

from klok import __version__
from klok.config import Config
from klok.focus import ART, countdown, notify, pomodoro_plan
from klok.formats import FORMATS, parse_import, render
from klok.mindful import (BREATHING_TAG, MEDITATION_TAG, PATTERNS, Checkin,
                          CheckinStore, MindfulError, breath_plan, breathe,
                          break_due, check_score, cycle_length, daily_averages,
                          describe_pattern, longest_streak, meditate,
                          parse_pattern, pattern_phases, rounds_for, sit_plan,
                          sparkline, streak)
from klok.model import Frame, normalise_tags
from klok.report import (Renderer, by_day, by_project, by_sheet, by_tag,
                         render_day_chart, render_gaps, render_log,
                         render_punchcard, render_report, render_summary,
                         render_totals)
from klok.storage import Store, StoreError
from klok.timeparse import (TimeParseError, format_duration, humanize_ago,
                            now_local, parse_datetime, parse_datetime_ex,
                            parse_duration,
                            parse_hours_window, parse_range)
from klok.utils import Term, die, render_table

ARCHIVE_PREFIX = "_"
# Options whose value is a time or duration and may therefore start with "-".
TIME_OPTIONS = {"-a", "--at", "--from", "--to", "--start", "--stop", "--now",
                "--min", "--work", "--break", "--long-break", "--round",
                "--for", "--interval-bell", "--warmup"}


class Context:
    """Everything a command needs: config, store, clock and renderer."""

    def __init__(self, args):
        self.config = Config(args.home)
        self.store = Store(self.config)
        self.now = parse_datetime(args.now) if args.now else now_local()
        color_mode = "never" if args.no_color else (args.color or self.config.get("general.color", "auto"))
        self.term = Term(color_mode)
        self.args = args
        round_minutes = args.round if args.round is not None else self.config.get_int("general.round", 0)
        self.round_minutes = round_minutes
        self.week_start = self.config.get("general.week_start", "monday")
        self.time_format = self.config.get("general.time_format", "24")
        self.renderer = Renderer(self.term, self.now, self.time_format, round_minutes,
                                 self.week_start, getattr(args, "duration_style", "short") or "short")

    # -- sheet resolution ---------------------------------------------
    def sheet_filter(self):
        """``None`` means "every sheet"; otherwise the sheet to restrict to."""
        if getattr(self.args, "all_sheets", False):
            return None
        return getattr(self.args, "sheet", None) or self.store.current_sheet

    def target_sheet(self) -> str:
        return getattr(self.args, "sheet", None) or self.store.current_sheet

    def echo(self, *parts) -> None:
        print(*parts)

    def range_of(self, default: str):
        tokens = getattr(self.args, "range_tokens", None) or []
        if getattr(self.args, "from_time", None) or getattr(self.args, "to_time", None):
            start = (parse_datetime(self.args.from_time, self.now) if self.args.from_time
                     else self.now.replace(hour=0, minute=0, second=0, microsecond=0))
            if not self.args.to_time:
                return start, self.now + timedelta(seconds=1)
            end, granularity = parse_datetime_ex(self.args.to_time, self.now)
            # A bare date as the end of a range means "up to and including".
            return start, end + timedelta(days=1) if granularity == "day" else end
        return parse_range(tokens, self.now, self.week_start, default=default)

    def selection(self, default_range: str):
        start, end = self.range_of(default_range)
        frames = self.store.select(
            start, end,
            sheet=self.sheet_filter(),
            projects=getattr(self.args, "project", None),
            tags=getattr(self.args, "tag", None),
            exclude_tags=getattr(self.args, "exclude_tag", None),
            contains=getattr(self.args, "contains", None),
            now=self.now,
        )
        return frames, start, end


# ------------------------------------------------------------------ helpers
def split_project_tags(tokens):
    """``acme +api +docs`` -> ``("acme", ["api", "docs"])``."""
    project, tags = "", []
    for token in tokens or []:
        token = str(token)
        if token.startswith("+"):
            tags.append(token[1:])
        elif not project:
            project = token
        else:
            tags.append(token)
    return project.strip(), normalise_tags(tags)


def resolve_time(ctx: Context, value, fallback=None):
    if value in (None, ""):
        return fallback if fallback is not None else ctx.now
    return parse_datetime(value, ctx.now)


def describe(ctx: Context, frame: Frame, prefix: str = "") -> str:
    colour = ctx.term.project_color(frame.project)
    label = ctx.term.paint(frame.project or "(no project)", colour, "bold")
    if frame.tags:
        label += " " + ctx.term.paint(" ".join("+" + tag for tag in frame.tags), "blue")
    return "%s%s" % (prefix, label)


def emit(ctx: Context, frames, text_builder, default_range: str = None):
    """Print either a machine format or the human rendering."""
    fmt = getattr(ctx.args, "format", None) or "text"
    if fmt != "text":
        print(render(frames, ctx.now, fmt))
        return 0
    print(text_builder())
    return 0


def check_project(ctx: Context, project: str) -> None:
    if not project or not ctx.config.get_bool("general.confirm_new_project"):
        return
    if project not in ctx.store.projects(None) and not getattr(ctx.args, "force", False):
        raise StoreError("unknown project %r (use --force, or unset general.confirm_new_project)" % project)


def stop_running(ctx: Context, when: datetime, note: str = "", sheet=None, clamp: bool = False):
    """Stop whatever is running, returning the frames that were closed.

    ``clamp`` is for the implicit stops that ``start``/``switch``/``restart``
    perform: an entry that began later than the new one simply collapses to
    zero rather than failing the whole command.
    """
    stopped = []
    for frame in list(ctx.store.frames):
        if not frame.running:
            continue
        if sheet is not None and frame.sheet != sheet:
            continue
        moment = when
        if moment < frame.start:
            if not clamp:
                raise StoreError("cannot stop %s before it started (%s)"
                                 % (frame.id, frame.start.isoformat()))
            moment = frame.start
        frame.stop = moment
        if note:
            frame.note = (frame.note + " " + note).strip() if frame.note else note
        frame.touch(ctx.now)
        stopped.append(frame)
    return stopped


# ----------------------------------------------------------------- tracking
def cmd_start(ctx: Context) -> int:
    args = ctx.args
    project, tags = split_project_tags(args.words)
    project = project or ctx.config.get("general.default_project", "")
    check_project(ctx, project)
    at = resolve_time(ctx, args.at)
    sheet = ctx.target_sheet()

    autostop = ctx.config.get_bool("general.autostop", True) and not args.no_stop
    stopped = []
    running = ctx.store.running(None)
    if running and not autostop:
        raise StoreError("%s is already running; stop it first or allow general.autostop" % running.id)
    if running:
        stopped = stop_running(ctx, at, sheet=None, clamp=True)

    frame = Frame(start=at, project=project, tags=tags, note=args.note or "", sheet=sheet)
    ctx.store.add(frame)
    ctx.store.save("start %s" % (project or frame.id))
    for frame_stopped in stopped:
        ctx.echo("Stopped %s (%s)" % (describe(ctx, frame_stopped),
                                      format_duration(frame_stopped.duration(ctx.now))))
    ctx.echo("Starting %s at %s [%s]" % (describe(ctx, frame),
                                         at.strftime("%Y-%m-%d %H:%M"),
                                         ctx.term.paint(frame.id, "grey")))
    return 0


def cmd_stop(ctx: Context) -> int:
    args = ctx.args
    at = resolve_time(ctx, args.at)
    sheet = None if args.all else ctx.sheet_filter()
    stopped = stop_running(ctx, at, note=args.note or "", sheet=sheet)
    if not stopped:
        ctx.echo("Nothing is running.")
        return 1
    ctx.store.save("stop")
    for frame in stopped:
        ctx.echo("Stopped %s after %s [%s]" % (describe(ctx, frame),
                                               format_duration(frame.duration(ctx.now)),
                                               ctx.term.paint(frame.id, "grey")))
    return 0


def cmd_cancel(ctx: Context) -> int:
    frame = ctx.store.running(ctx.sheet_filter()) or ctx.store.running(None)
    if not frame:
        ctx.echo("Nothing is running.")
        return 1
    ctx.store.remove(frame)
    ctx.store.save("cancel")
    ctx.echo("Cancelled %s (%s discarded)" % (describe(ctx, frame),
                                              format_duration(frame.duration(ctx.now))))
    return 0


def cmd_status(ctx: Context) -> int:
    frame = ctx.store.running(ctx.sheet_filter()) or ctx.store.running(None)
    if getattr(ctx.args, "format", "text") == "json":
        payload = {"running": bool(frame)}
        if frame:
            payload.update(frame.to_dict())
            payload["elapsed_seconds"] = int(frame.duration(ctx.now).total_seconds())
        print(json.dumps(payload, indent=2))
        return 0 if frame else 1
    if not frame:
        last = ctx.store.last(ctx.sheet_filter())
        if last and not ctx.args.quiet:
            ctx.echo("Nothing running. Last: %s ended %s." %
                     (describe(ctx, last), humanize_ago(ctx.now - last.end_or(ctx.now))))
        elif not ctx.args.quiet:
            ctx.echo("Nothing running.")
        return 1
    elapsed = frame.duration(ctx.now)
    today_start = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = ctx.store.select(today_start, today_start + timedelta(days=1),
                             sheet=frame.sheet, now=ctx.now)
    today_total = sum((f.duration(ctx.now) for f in today), timedelta(0))
    ctx.echo("%s  %s  (started %s, %s)" % (
        describe(ctx, frame),
        ctx.term.paint(format_duration(elapsed), "yellow", "bold"),
        frame.start.strftime("%H:%M"),
        humanize_ago(ctx.now - frame.start)))
    if frame.note:
        ctx.echo("  note: %s" % frame.note)
    ctx.echo("  sheet %s  |  today %s  |  id %s" % (
        frame.sheet, format_duration(today_total), ctx.term.paint(frame.id, "grey")))
    threshold = ctx.config.get("mindful.break_after", "")
    if threshold and break_due(frame, parse_duration(threshold), ctx.now):
        ctx.echo(ctx.term.paint(
            "  You have been at this %s straight - `klok breathe` takes about a minute."
            % format_duration(elapsed), "dim"))
    return 0


def cmd_restart(ctx: Context) -> int:
    args = ctx.args
    reference = args.ref or "@"
    sheet = ctx.sheet_filter()
    pool = [frame for frame in ctx.store.frames if sheet is None or frame.sheet == sheet]
    if not pool:
        raise StoreError("nothing to restart yet")
    frame = ctx.store.by_id(reference, sheet) if args.ref else (
        ctx.store.running(sheet) or pool[-1])
    at = resolve_time(ctx, args.at)
    if frame.running:
        ctx.echo("%s is already running." % describe(ctx, frame))
        return 0
    stopped = stop_running(ctx, at, sheet=None, clamp=True)
    fresh = Frame(start=at, project=frame.project, tags=list(frame.tags),
                  note=frame.note if args.keep_note else "", sheet=frame.sheet)
    ctx.store.add(fresh)
    ctx.store.save("restart %s" % (frame.project or frame.id))
    for item in stopped:
        ctx.echo("Stopped %s (%s)" % (describe(ctx, item), format_duration(item.duration(ctx.now))))
    ctx.echo("Restarted %s at %s [%s]" % (describe(ctx, fresh), at.strftime("%H:%M"),
                                          ctx.term.paint(fresh.id, "grey")))
    return 0


def cmd_track(ctx: Context) -> int:
    """Record a finished interval after the fact."""
    args = ctx.args
    tokens = list(args.words)
    start_token, end_token = args.from_time, args.to_time
    if not start_token and tokens:
        # Support: klok track 09:00 to 11:30 acme +api
        lowered = [token.lower() for token in tokens]
        if "to" in lowered:
            index = lowered.index("to")
            start_token = " ".join(tokens[:index])
            rest = tokens[index + 1:]
            end_token = rest[0] if rest else None
            tokens = rest[1:]
        elif "-" in lowered and lowered.index("-") + 1 < len(tokens):
            index = lowered.index("-")
            start_token, end_token = " ".join(tokens[:index]), tokens[index + 1]
            tokens = tokens[index + 2:]
    if not start_token:
        raise StoreError("track needs a time range, e.g. `klok track 09:00 to 11:30 acme +api`")

    start = parse_datetime(start_token, ctx.now)
    if end_token:
        end = parse_datetime(end_token, ctx.now)
    elif args.duration:
        end = start + parse_duration(args.duration)
    else:
        end = ctx.now
    if end < start:
        end += timedelta(days=1)
    project, tags = split_project_tags(tokens)
    project = project or ctx.config.get("general.default_project", "")
    check_project(ctx, project)
    frame = Frame(start=start, stop=end, project=project, tags=tags,
                  note=args.note or "", sheet=ctx.target_sheet())
    ctx.store.add(frame)
    ctx.store.save("track %s" % (project or frame.id))
    ctx.echo("Tracked %s from %s to %s (%s) [%s]" % (
        describe(ctx, frame), start.strftime("%Y-%m-%d %H:%M"), end.strftime("%H:%M"),
        format_duration(frame.duration(ctx.now)), ctx.term.paint(frame.id, "grey")))
    return 0


def cmd_add(ctx: Context) -> int:
    """utt-style: close the open stretch of time with a named activity."""
    args = ctx.args
    project, tags = split_project_tags(args.words)
    if not project and not tags:
        raise StoreError("add needs an activity name, e.g. `klok add acme +api`")
    end = resolve_time(ctx, args.at)
    sheet = ctx.target_sheet()
    running = ctx.store.running(None)
    if running:
        start = running.start
        ctx.store.remove(running)
    else:
        previous = [frame for frame in ctx.store.frames
                    if frame.sheet == sheet and frame.stop and frame.stop <= end]
        if not previous:
            raise StoreError("no earlier entry to continue from; use `klok track` instead")
        start = max(frame.stop for frame in previous)
    if end <= start:
        raise StoreError("nothing to add: the previous entry already ends at %s" % start.strftime("%H:%M"))
    frame = Frame(start=start, stop=end, project=project, tags=tags,
                  note=args.note or "", sheet=sheet)
    ctx.store.add(frame)
    ctx.store.save("add %s" % (project or frame.id))
    ctx.echo("Added %s %s to %s (%s) [%s]" % (
        describe(ctx, frame), start.strftime("%H:%M"), end.strftime("%H:%M"),
        format_duration(frame.duration(ctx.now)), ctx.term.paint(frame.id, "grey")))
    return 0


def cmd_switch(ctx: Context) -> int:
    args = ctx.args
    project, tags = split_project_tags(args.words)
    if not project and not tags:
        raise StoreError("switch needs a project, e.g. `klok switch acme +api`")
    at = resolve_time(ctx, args.at)
    stopped = stop_running(ctx, at, sheet=None, clamp=True)
    check_project(ctx, project)
    frame = Frame(start=at, project=project, tags=tags, note=args.note or "",
                  sheet=ctx.target_sheet())
    ctx.store.add(frame)
    ctx.store.save("switch %s" % (project or frame.id))
    for item in stopped:
        ctx.echo("Stopped %s (%s)" % (describe(ctx, item), format_duration(item.duration(ctx.now))))
    ctx.echo("Switched to %s at %s [%s]" % (describe(ctx, frame), at.strftime("%H:%M"),
                                            ctx.term.paint(frame.id, "grey")))
    return 0


def cmd_stretch(ctx: Context) -> int:
    """Pull an entry's start back to the end of the one before it."""
    sheet = ctx.sheet_filter()
    frame = ctx.store.by_id(ctx.args.ref or "@", sheet)
    if ctx.args.by:
        new_start = frame.start - parse_duration(ctx.args.by)
    else:
        earlier = [item for item in ctx.store.frames
                   if item.sheet == frame.sheet and item.id != frame.id and item.end_or(ctx.now) <= frame.start]
        if not earlier:
            raise StoreError("no earlier entry to stretch back to")
        new_start = max(item.end_or(ctx.now) for item in earlier)
    if new_start >= frame.end_or(ctx.now):
        raise StoreError("stretching that far would leave a negative duration")
    gained = frame.start - new_start
    frame.start = new_start
    frame.touch(ctx.now)
    ctx.store.save("stretch %s" % frame.id)
    ctx.echo("Stretched %s back %s; it now starts at %s (%s)" % (
        ctx.term.paint(frame.id, "grey"), format_duration(gained),
        frame.start.strftime("%H:%M"), format_duration(frame.duration(ctx.now))))
    return 0


# ------------------------------------------------------------------ editing
def _editor(ctx: Context):
    return (ctx.config.get("general.editor") or os.environ.get("VISUAL")
            or os.environ.get("EDITOR") or "vi")


def _open_editor(ctx: Context, text: str, suffix: str = ".json") -> str:
    handle, path = tempfile.mkstemp(prefix="klok-", suffix=suffix)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        command = _editor(ctx)
        subprocess.call("%s %s" % (command, path), shell=True)
        with open(path, "r", encoding="utf-8") as stream:
            return stream.read()
    finally:
        os.unlink(path)


def cmd_edit(ctx: Context) -> int:
    args = ctx.args
    sheet = ctx.sheet_filter()
    if args.range_tokens or args.all:
        frames, _, _ = ctx.selection(":day")
        if not frames:
            raise StoreError("no entries match that range")
    else:
        frames = [ctx.store.by_id(args.ref or "@", sheet)]

    inline = any([args.start, args.stop, args.set_project is not None,
                  args.note is not None, args.set_tags])
    if inline:
        for frame in frames:
            if args.start:
                frame.start = parse_datetime(args.start, ctx.now)
            if args.stop:
                frame.stop = None if args.stop.lower() in ("none", "running") else parse_datetime(args.stop, ctx.now)
            if args.set_project is not None:
                frame.project = args.set_project
            if args.note is not None:
                frame.note = args.note
            if args.set_tags:
                frame.tags = normalise_tags(args.set_tags)
            if frame.stop and frame.stop < frame.start:
                raise StoreError("%s would end before it starts" % frame.id)
            frame.touch(ctx.now)
        ctx.store.save("edit")
        ctx.echo("Updated %d entr%s." % (len(frames), "y" if len(frames) == 1 else "ies"))
        return 0

    payload = json.dumps([frame.to_dict() for frame in frames], indent=2, ensure_ascii=False)
    edited = _open_editor(ctx, payload + "\n")
    try:
        parsed = json.loads(edited)
    except ValueError as exc:
        raise StoreError("the edited file is not valid JSON (%s); nothing was changed" % exc)
    if isinstance(parsed, dict):
        parsed = [parsed]
    kept = {frame.id for frame in frames}
    remaining = [frame for frame in ctx.store.frames if frame.id not in kept]
    replacements = []
    for item in parsed:
        frame = Frame.from_dict(item)
        if frame.stop and frame.stop < frame.start:
            raise StoreError("%s would end before it starts; nothing was changed" % frame.id)
        frame.touch(ctx.now)
        replacements.append(frame)
    ctx.store.replace(remaining + replacements)
    ctx.store.save("edit")
    removed = len(frames) - len(replacements)
    ctx.echo("Saved %d entr%s%s." % (len(replacements), "y" if len(replacements) == 1 else "ies",
                                     ", removed %d" % removed if removed > 0 else ""))
    return 0


def cmd_annotate(ctx: Context) -> int:
    """``klok annotate ID text`` and ``klok annotate text`` both work."""
    words = list(ctx.args.words)
    reference = ctx.args.ref or "@"
    try:
        frame = ctx.store.by_id(reference, ctx.sheet_filter())
    except StoreError:
        if not ctx.args.ref:
            raise
        # The first word was prose, not an entry id.
        words.insert(0, ctx.args.ref)
        frame = ctx.store.by_id("@", ctx.sheet_filter())
    text = " ".join(words).strip()
    if ctx.args.append and frame.note:
        frame.note = "%s %s" % (frame.note, text)
    else:
        frame.note = text
    frame.touch(ctx.now)
    ctx.store.save("annotate %s" % frame.id)
    ctx.echo("Annotated %s: %s" % (ctx.term.paint(frame.id, "grey"), frame.note or "(cleared)"))
    return 0


def cmd_tag(ctx: Context) -> int:
    frame = ctx.store.by_id(ctx.args.ref or "@", ctx.sheet_filter())
    added, removed = [], []
    for token in ctx.args.words:
        if token.startswith("-") or token.startswith("~"):
            name = token[1:]
            if name in frame.tags:
                frame.tags.remove(name)
                removed.append(name)
        else:
            name = token.lstrip("+")
            if name and name not in frame.tags:
                frame.tags.append(name)
                added.append(name)
    frame.touch(ctx.now)
    ctx.store.save("tag %s" % frame.id)
    parts = []
    if added:
        parts.append("added %s" % ", ".join(added))
    if removed:
        parts.append("removed %s" % ", ".join(removed))
    ctx.echo("%s: %s" % (ctx.term.paint(frame.id, "grey"), "; ".join(parts) or "no change"))
    return 0


def cmd_untag(ctx: Context) -> int:
    ctx.args.words = ["~" + token.lstrip("+-~") for token in ctx.args.words]
    return cmd_tag(ctx)


def cmd_rename(ctx: Context) -> int:
    kind, old, new = ctx.args.kind, ctx.args.old, ctx.args.new
    touched = 0
    for frame in ctx.store.frames:
        if kind == "project" and frame.project == old:
            frame.project = new
            touched += 1
        elif kind == "tag" and old in frame.tags:
            frame.tags = normalise_tags([new if tag == old else tag for tag in frame.tags])
            touched += 1
        elif kind == "sheet" and frame.sheet == old:
            frame.sheet = new
            touched += 1
        else:
            continue
        frame.touch(ctx.now)
    if not touched:
        raise StoreError("no entries use the %s %r" % (kind, old))
    ctx.store.save("rename %s" % kind)
    ctx.echo("Renamed %s %s -> %s in %d entr%s." % (kind, old, new, touched,
                                                    "y" if touched == 1 else "ies"))
    return 0


def cmd_move(ctx: Context) -> int:
    frame = ctx.store.by_id(ctx.args.ref, ctx.sheet_filter())
    new_start = parse_datetime(ctx.args.time, ctx.now)
    shift = new_start - frame.start
    frame.start = new_start
    if frame.stop:
        frame.stop += shift
    frame.touch(ctx.now)
    ctx.store.save("move %s" % frame.id)
    ctx.echo("Moved %s by %s; now %s%s" % (
        ctx.term.paint(frame.id, "grey"), format_duration(shift),
        frame.start.strftime("%Y-%m-%d %H:%M"),
        " to " + frame.stop.strftime("%H:%M") if frame.stop else ""))
    return 0


def cmd_resize(ctx: Context) -> int:
    """Shared implementation of lengthen and shorten."""
    frame = ctx.store.by_id(ctx.args.ref, ctx.sheet_filter())
    delta = parse_duration(ctx.args.duration)
    if ctx.args.command == "shorten":
        delta = -delta
    if frame.running:
        raise StoreError("%s is still running; stop it before resizing" % frame.id)
    if ctx.args.start_side:
        new_start = frame.start - delta
        if new_start >= frame.stop:
            raise StoreError("that would leave a negative duration")
        frame.start = new_start
    else:
        new_stop = frame.stop + delta
        if new_stop <= frame.start:
            raise StoreError("that would leave a negative duration")
        frame.stop = new_stop
    frame.touch(ctx.now)
    ctx.store.save("%s %s" % (ctx.args.command, frame.id))
    ctx.echo("%s is now %s (%s to %s)" % (
        ctx.term.paint(frame.id, "grey"), format_duration(frame.duration(ctx.now)),
        frame.start.strftime("%H:%M"), frame.stop.strftime("%H:%M")))
    return 0


def cmd_fill(ctx: Context) -> int:
    """Grow an entry until it touches its neighbours."""
    frame = ctx.store.by_id(ctx.args.ref, ctx.sheet_filter())
    siblings = [item for item in ctx.store.frames if item.sheet == frame.sheet and item.id != frame.id]
    before = [item.end_or(ctx.now) for item in siblings if item.end_or(ctx.now) <= frame.start]
    after = [item.start for item in siblings if item.start >= frame.end_or(ctx.now)]
    day_start = frame.start.replace(hour=0, minute=0, second=0, microsecond=0)
    new_start = max(before) if before else day_start
    frame.start = new_start
    if not frame.running:
        frame.stop = min(after) if after else min(ctx.now, day_start + timedelta(days=1))
    frame.touch(ctx.now)
    ctx.store.save("fill %s" % frame.id)
    ctx.echo("Filled %s: %s to %s (%s)" % (
        ctx.term.paint(frame.id, "grey"), frame.start.strftime("%H:%M"),
        frame.stop.strftime("%H:%M") if frame.stop else "now",
        format_duration(frame.duration(ctx.now))))
    return 0


def cmd_join(ctx: Context) -> int:
    sheet = ctx.sheet_filter()
    frames = [ctx.store.by_id(ref, sheet) for ref in ctx.args.refs]
    if len(frames) < 2:
        raise StoreError("join needs at least two entries")
    frames.sort(key=lambda frame: frame.start)
    first = frames[0]
    last_end = max(frame.end_or(ctx.now) for frame in frames)
    running = any(frame.running for frame in frames)
    tags, notes = [], []
    for frame in frames:
        tags.extend(frame.tags)
        if frame.note:
            notes.append(frame.note)
    merged = Frame(start=first.start, stop=None if running else last_end,
                   project=first.project, tags=normalise_tags(tags),
                   note="; ".join(dict.fromkeys(notes)), sheet=first.sheet, id=first.id)
    for frame in frames:
        ctx.store.remove(frame)
    ctx.store.add(merged)
    ctx.store.save("join")
    ctx.echo("Joined %d entries into %s (%s)" % (
        len(frames), ctx.term.paint(merged.id, "grey"), format_duration(merged.duration(ctx.now))))
    return 0


def cmd_split(ctx: Context) -> int:
    frame = ctx.store.by_id(ctx.args.ref, ctx.sheet_filter())
    end = frame.end_or(ctx.now)
    if ctx.args.at:
        cut = parse_datetime(ctx.args.at, ctx.now)
        if not (frame.start < cut < end):
            raise StoreError("%s is outside the entry" % cut.strftime("%H:%M"))
        cuts = [cut]
    else:
        parts = max(2, ctx.args.into)
        step = (end - frame.start) / parts
        cuts = [frame.start + step * index for index in range(1, parts)]
    boundaries = [frame.start] + cuts + [end]
    pieces = []
    for index in range(len(boundaries) - 1):
        pieces.append(Frame(start=boundaries[index],
                            stop=None if (frame.running and index == len(boundaries) - 2) else boundaries[index + 1],
                            project=frame.project, tags=list(frame.tags), note=frame.note,
                            sheet=frame.sheet))
    ctx.store.remove(frame)
    for piece in pieces:
        ctx.store.add(piece)
    ctx.store.save("split %s" % frame.id)
    ctx.echo("Split into %d entries: %s" % (len(pieces), ", ".join(piece.id for piece in pieces)))
    return 0


def cmd_delete(ctx: Context) -> int:
    sheet = ctx.sheet_filter()
    frames = [ctx.store.by_id(ref, sheet) for ref in ctx.args.refs]
    for frame in frames:
        ctx.store.remove(frame)
    ctx.store.save("delete")
    for frame in frames:
        ctx.echo("Deleted %s %s (%s)" % (ctx.term.paint(frame.id, "grey"), describe(ctx, frame),
                                         format_duration(frame.duration(ctx.now))))
    ctx.echo("Use `klok undo` to put %s back." % ("it" if len(frames) == 1 else "them"))
    return 0


def cmd_undo(ctx: Context) -> int:
    label = ctx.store.undo()
    if label is None:
        ctx.echo("Nothing to undo.")
        return 1
    ctx.echo("Undid: %s" % label)
    return 0


# ---------------------------------------------------------------- reporting
def cmd_log(ctx: Context) -> int:
    frames, start, end = ctx.selection(":week")
    if ctx.args.limit:
        frames = frames[-ctx.args.limit:]
    if ctx.args.reverse:
        frames = list(reversed(frames))
    return emit(ctx, frames, lambda: render_log(frames, ctx.renderer, show_notes=not ctx.args.no_notes))


def cmd_summary(ctx: Context) -> int:
    frames, start, end = ctx.selection(":day")
    return emit(ctx, frames, lambda: render_summary(frames, ctx.renderer, start, end))


def cmd_report(ctx: Context) -> int:
    frames, start, end = ctx.selection(":week")
    return emit(ctx, frames, lambda: render_report(frames, ctx.renderer, start, end,
                                                   show_tags=not ctx.args.no_tags,
                                                   show_entries=ctx.args.entries))


def cmd_chart(ctx: Context) -> int:
    frames, start, end = ctx.selection(":week")
    mode = ctx.args.by
    if mode == "day":
        text = render_day_chart(frames, ctx.renderer, start, end)
    elif mode == "project":
        text = render_totals(by_project(frames, ctx.renderer), ctx.renderer, "By project")
    elif mode == "tag":
        text = render_totals(by_tag(frames, ctx.renderer), ctx.renderer, "By tag")
    elif mode == "sheet":
        text = render_totals(by_sheet(frames, ctx.renderer), ctx.renderer, "By sheet")
    else:
        text = render_punchcard(frames, ctx.renderer)
    print(text)
    return 0


def cmd_gaps(ctx: Context) -> int:
    start, end = ctx.range_of(":day")
    minimum = parse_duration(ctx.args.min) if ctx.args.min else timedelta(minutes=5)
    gaps = ctx.store.gaps(start, end, sheet=ctx.sheet_filter(), now=ctx.now, minimum=minimum)
    window = parse_hours_window(ctx.config.get("exclusions.hours", ""))
    excluded_days = {name.strip().lower()[:3] for name in ctx.config.get_list("exclusions.days")}
    holidays = set(ctx.config.get_list("exclusions.holidays"))
    if window or excluded_days or holidays:
        kept = []
        for gap_start, gap_end in gaps:
            if gap_start.strftime("%a").lower() in excluded_days:
                continue
            if gap_start.strftime("%Y-%m-%d") in holidays:
                continue
            if window:
                low = gap_start.replace(hour=window[0].hour, minute=window[0].minute, second=0)
                high = gap_start.replace(hour=window[1].hour, minute=window[1].minute, second=0)
                gap_start, gap_end = max(gap_start, low), min(gap_end, high)
                if gap_end <= gap_start or gap_end - gap_start < minimum:
                    continue
            kept.append((gap_start, gap_end))
        gaps = kept
    print(render_gaps(gaps, ctx.renderer))
    return 0


def cmd_stats(ctx: Context) -> int:
    frames, start, end = ctx.selection(":month")
    if not frames:
        ctx.echo("No time tracked in this range.")
        return 1
    renderer = ctx.renderer
    totals = by_day(frames, renderer)
    grand = sum(totals.values(), timedelta(0))
    longest = max(frames, key=lambda frame: renderer.duration_of(frame))
    projects = by_project(frames, renderer)
    busiest_day = max(totals.items(), key=lambda item: item[1])

    streak, cursor = 0, ctx.now.date()
    tracked_days = set(totals)
    while cursor in tracked_days:
        streak += 1
        cursor -= timedelta(days=1)

    rows = [
        ["Range", "%s to %s" % (start.strftime("%Y-%m-%d"), (end - timedelta(seconds=1)).strftime("%Y-%m-%d"))],
        ["Entries", str(len(frames))],
        ["Tracked", format_duration(grand)],
        ["Days tracked", str(len(totals))],
        ["Average per tracked day", format_duration(grand / len(totals))],
        ["Busiest day", "%s (%s)" % (busiest_day[0].isoformat(), format_duration(busiest_day[1]))],
        ["Longest entry", "%s (%s)" % (longest.project or "(no project)",
                                       format_duration(renderer.duration_of(longest)))],
        ["Projects", str(len(projects))],
        ["Top project", "%s (%s)" % (next(iter(projects)), format_duration(next(iter(projects.values()))))
                        if projects else "-"],
        ["Current streak", "%d day%s" % (streak, "" if streak == 1 else "s")],
    ]
    print(render_table(rows, aligns=["left", "left"]))
    return 0


def cmd_projects(ctx: Context) -> int:
    frames, _, _ = ctx.selection(":all")
    totals = by_project(frames, ctx.renderer)
    if not totals:
        ctx.echo("No projects recorded yet.")
        return 1
    if ctx.args.quiet:
        for name in sorted(totals):
            print(name)
        return 0
    rows = [[name, format_duration(length),
             str(len([frame for frame in frames if (frame.project or "(no project)") == name]))]
            for name, length in totals.items()]
    print(render_table(rows, headers=["Project", "Time", "Entries"],
                       aligns=["left", "right", "right"], term=ctx.term))
    return 0


def cmd_tags(ctx: Context) -> int:
    frames, _, _ = ctx.selection(":all")
    totals = by_tag(frames, ctx.renderer)
    if not totals:
        ctx.echo("No tags recorded yet.")
        return 1
    if ctx.args.quiet:
        for name in sorted(totals):
            print(name)
        return 0
    rows = [[name, format_duration(length)] for name, length in totals.items()]
    print(render_table(rows, headers=["Tag", "Time"], aligns=["left", "right"], term=ctx.term))
    return 0


def cmd_export(ctx: Context) -> int:
    frames, _, _ = ctx.selection(":all")
    text = render(frames, ctx.now, ctx.args.format or "json")
    if ctx.args.output:
        with open(ctx.args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        ctx.echo("Wrote %d entries to %s" % (len(frames), ctx.args.output))
    else:
        print(text)
    return 0


def cmd_import(ctx: Context) -> int:
    text = (open(ctx.args.file, encoding="utf-8").read() if ctx.args.file and ctx.args.file != "-"
            else sys.stdin.read())
    incoming = parse_import(text)
    if not incoming:
        ctx.echo("Nothing to import.")
        return 1
    existing = {frame.id for frame in ctx.store.frames}
    added, skipped = 0, 0
    for frame in incoming:
        if frame.id in existing:
            skipped += 1
            continue
        if ctx.args.sheet:
            frame.sheet = ctx.args.sheet
        ctx.store.add(frame)
        added += 1
    ctx.store.save("import")
    ctx.echo("Imported %d entries (%d already present)." % (added, skipped))
    return 0


# ------------------------------------------------------------------- sheets
def cmd_sheet(ctx: Context) -> int:
    if not ctx.args.name:
        ctx.echo(ctx.store.current_sheet)
        return 0
    previous = ctx.store.current_sheet
    name = ctx.store.set_sheet(ctx.args.name)
    ctx.echo("Switched to sheet %s (was %s)." % (ctx.term.paint(name, "bold"), previous))
    return 0


def cmd_sheets(ctx: Context) -> int:
    current = ctx.store.current_sheet
    rows = []
    for name in ctx.store.sheets():
        frames = [frame for frame in ctx.store.frames if frame.sheet == name]
        total = sum((ctx.renderer.duration_of(frame) for frame in frames), timedelta(0))
        running = any(frame.running for frame in frames)
        marker = "*" if name == current else ""
        rows.append([marker, ctx.term.paint(name, "bold") if marker else name,
                     str(len(frames)), format_duration(total), "running" if running else ""])
    print(render_table(rows, headers=["", "Sheet", "Entries", "Time", ""],
                       aligns=["left", "left", "right", "right", "left"], term=ctx.term))
    return 0


def cmd_archive(ctx: Context) -> int:
    frames, _, _ = ctx.selection(":all")
    frames = [frame for frame in frames if not frame.sheet.startswith(ARCHIVE_PREFIX)]
    if not frames:
        ctx.echo("Nothing to archive in this range.")
        return 1
    for frame in frames:
        frame.sheet = ARCHIVE_PREFIX + frame.sheet
        frame.touch(ctx.now)
    ctx.store.save("archive")
    ctx.echo("Archived %d entries. `klok log --all-sheets` still shows them." % len(frames))
    return 0


# ------------------------------------------------------------------- config
def cmd_config(ctx: Context) -> int:
    action = ctx.args.action
    if action == "list" or (action is None):
        for key, value in ctx.config.items():
            print("%s = %s" % (key, value))
        return 0
    if action == "path":
        print(ctx.config.path)
        return 0
    if action == "edit":
        ctx.config.save()
        subprocess.call("%s %s" % (_editor(ctx), ctx.config.path), shell=True)
        return 0
    if action == "get":
        value = ctx.config.get(ctx.args.key)
        if value is None:
            raise StoreError("no such config key: %s" % ctx.args.key)
        print(value)
        return 0
    if action == "set":
        ctx.config.set(ctx.args.key, ctx.args.value)
        ctx.config.save()
        ctx.echo("%s = %s" % (ctx.args.key, ctx.args.value))
        return 0
    if action == "unset":
        if not ctx.config.unset(ctx.args.key):
            raise StoreError("no such config key: %s" % ctx.args.key)
        ctx.config.save()
        ctx.echo("Removed %s" % ctx.args.key)
        return 0
    raise StoreError("unknown config action %r" % action)


def cmd_where(ctx: Context) -> int:
    rows = [["data", str(ctx.config.home)], ["frames", str(ctx.config.frames_path)],
            ["config", str(ctx.config.path)], ["state", str(ctx.config.state_path)],
            ["undo", str(ctx.config.undo_path)]]
    print(render_table(rows, aligns=["left", "left"]))
    return 0


# -------------------------------------------------------------------- focus
def _focus_frame(ctx: Context, project, tags, note, start, end, sheet):
    frame = Frame(start=start, stop=end, project=project, tags=tags, note=note, sheet=sheet)
    ctx.store.add(frame)
    ctx.store.save("focus")
    return frame


def cmd_timer(ctx: Context) -> int:
    duration = parse_duration(ctx.args.duration)
    label = ctx.args.message or "Timer"
    track = ctx.args.project is not None and not ctx.args.no_track
    started = now_local()
    finished = countdown(duration, label, ctx.term, art=ctx.args.art, big=ctx.args.big,
                         quiet=ctx.args.quiet,
                         notify_on_end=ctx.config.get_bool("focus.notify", True),
                         bell=ctx.config.get_bool("focus.bell", True))
    if track:
        project, tags = split_project_tags([ctx.args.project] + ["+" + tag for tag in (ctx.args.tag or [])])
        end = now_local()
        frame = _focus_frame(ctx, project, tags, ctx.args.message or "", started, end, ctx.target_sheet())
        ctx.echo("Recorded %s (%s)" % (describe(ctx, frame), format_duration(frame.duration(ctx.now))))
    return 0 if finished else 130


def cmd_pomodoro(ctx: Context) -> int:
    config = ctx.config
    work = parse_duration(ctx.args.work or config.get("focus.work", "25m"))
    short_break = parse_duration(ctx.args.short_break or config.get("focus.break", "5m"))
    long_break = parse_duration(ctx.args.long_break or config.get("focus.long_break", "15m"))
    rounds = ctx.args.rounds or config.get_int("focus.rounds", 4)
    track = config.get_bool("focus.track", True) and not ctx.args.no_track
    project, tags = split_project_tags(ctx.args.words)
    completed = 0

    for kind, label, duration in pomodoro_plan(rounds, work, short_break, long_break):
        if kind != "work" and ctx.args.no_breaks:
            continue
        started = now_local()
        finished = countdown(duration, label, ctx.term, art=ctx.args.art, big=ctx.args.big,
                             quiet=ctx.args.quiet,
                             notify_on_end=config.get_bool("focus.notify", True),
                             bell=config.get_bool("focus.bell", True))
        if kind == "work":
            elapsed = now_local() - started
            if track and elapsed.total_seconds() >= 60:
                _focus_frame(ctx, project, tags, label, started, now_local(), ctx.target_sheet())
            if finished:
                completed += 1
        if not finished:
            ctx.echo("Stopped after %d completed round%s." % (completed, "" if completed == 1 else "s"))
            return 130
        if kind != "work" and not ctx.args.no_prompt and duration.total_seconds() > 0:
            try:
                input("Press enter to start the next round (Ctrl-C to stop)... ")
            except (KeyboardInterrupt, EOFError):
                ctx.echo("")
                return 130
    notify("klok", "Pomodoro finished: %d rounds" % completed,
           enabled=config.get_bool("focus.notify", True), bell=config.get_bool("focus.bell", True))
    ctx.echo("Done: %d round%s of %s." % (completed, "" if completed == 1 else "s", format_duration(work)))
    return 0


# ---------------------------------------------------------------- mindfulness
def practice_sheet(ctx: Context) -> str:
    """Practice lives on its own sheet unless the user says otherwise."""
    return getattr(ctx.args, "sheet", None) or ctx.config.get("mindful.sheet", "wellbeing")


def record_practice(ctx: Context, tag: str, started: datetime, ended: datetime,
                    note: str = "", extra_tags=()):
    """Store a finished session as an ordinary frame, or return None."""
    if getattr(ctx.args, "no_track", False) or not ctx.config.get_bool("mindful.track", True):
        return None
    if (ended - started).total_seconds() < 5:
        return None
    frame = Frame(start=started, stop=ended,
                  project=ctx.config.get("mindful.project", "mindfulness"),
                  tags=normalise_tags([tag] + list(extra_tags)),
                  note=note or "", sheet=practice_sheet(ctx))
    ctx.store.add(frame)
    ctx.store.save("practice %s" % tag)
    return frame


def cmd_breathe(ctx: Context) -> int:
    """A guided breathing pacer."""
    args = ctx.args
    if args.list:
        rows = [[name, "-".join("%g" % value for value in values[:4]), values[4]]
                for name, values in sorted(PATTERNS.items())]
        print(render_table(rows, headers=["Pattern", "Counts", "What it is"],
                           aligns=["left", "left", "left"], term=ctx.term))
        print("\nAny counts work too: `klok breathe 4-7-8` or `klok breathe 5-2-7-2`.")
        return 0

    pattern = parse_pattern(args.pattern or (args.words[0] if args.words else None)
                            or ctx.config.get("mindful.pattern", "box"))
    if args.for_time:
        rounds = rounds_for(pattern, parse_duration(args.for_time))
    else:
        rounds = args.rounds or ctx.config.get_int("mindful.breath_rounds", 6)
    if rounds < 1:
        raise MindfulError("a session needs at least one round")
    total = cycle_length(pattern) * rounds

    if args.plan:
        ctx.echo("Pattern:  %s" % describe_pattern(pattern))
        ctx.echo("Rounds:   %d   Cycle: %s   Total: %s"
                 % (rounds, format_duration(cycle_length(pattern)), format_duration(total)))
        rows = [[label, "%gs" % seconds] for _, label, seconds in pattern_phases(pattern)]
        print(render_table(rows, aligns=["left", "right"], indent="  "))
        ctx.echo("Steps:    %d" % len(breath_plan(pattern, rounds)))
        return 0

    started = now_local()
    ctx.echo("%s   %s" % (ctx.term.paint("Breathing", "cyan", "bold"), describe_pattern(pattern)))
    finished = breathe(pattern, rounds, ctx.term, quiet=args.quiet,
                       bell=ctx.config.get_bool("focus.bell", True),
                       notify_on_end=ctx.config.get_bool("focus.notify", True))
    ended = now_local()
    frame = record_practice(ctx, BREATHING_TAG, started, ended,
                            note=args.note or pattern[4], extra_tags=args.tag or [])
    if not finished:
        ctx.echo("Stopped after %s." % format_duration(ended - started))
    if frame:
        ctx.echo("Recorded %s (%s) on sheet %s"
                 % (describe(ctx, frame), format_duration(frame.duration(ended)), frame.sheet))
    return 0 if finished else 130


def cmd_meditate(ctx: Context) -> int:
    """A timed sit, with a bell at the end and optionally along the way."""
    args = ctx.args
    length = parse_duration(args.for_time or args.duration
                            or ctx.config.get("mindful.default_sit", "10m"))
    if length.total_seconds() <= 0:
        raise MindfulError("a sit needs a positive length")
    interval_raw = args.interval_bell if args.interval_bell is not None else ctx.config.get("mindful.interval_bell", "")
    interval = parse_duration(interval_raw) if interval_raw else None
    warmup_raw = args.warmup if args.warmup is not None else ctx.config.get("mindful.warmup", "")
    warmup = parse_duration(warmup_raw) if warmup_raw else None
    guidance = ctx.config.get_bool("mindful.guidance", True) and not args.no_guidance

    if args.plan:
        marks = sit_plan(length, interval, warmup)
        ctx.echo("Sit:      %s" % format_duration(length))
        ctx.echo("Bells:    %s" % ", ".join(format_duration(timedelta(seconds=mark)) for mark in marks))
        ctx.echo("Warm-up:  %s" % (format_duration(warmup) if warmup else "none"))
        ctx.echo("Guidance: %s" % ("on" if guidance else "off"))
        ctx.echo("Recorded: %s +%s on sheet %s"
                 % (ctx.config.get("mindful.project", "mindfulness"), MEDITATION_TAG,
                    practice_sheet(ctx)))
        return 0

    started = now_local()
    finished = meditate(length, ctx.term, interval=interval, warmup=warmup, guidance=guidance,
                        quiet=args.quiet, bell=ctx.config.get_bool("focus.bell", True),
                        notify_on_end=ctx.config.get_bool("focus.notify", True))
    ended = now_local()
    frame = record_practice(ctx, MEDITATION_TAG, started, ended,
                            note=args.note or "", extra_tags=args.tag or [])
    if not finished:
        ctx.echo("Ended early after %s." % format_duration(ended - started))
    if frame:
        ctx.echo("Recorded %s (%s) on sheet %s"
                 % (describe(ctx, frame), format_duration(frame.duration(ended)), frame.sheet))
    return 0 if finished else 130


def cmd_checkin(ctx: Context) -> int:
    """Note how you are doing, on three 1-5 scales."""
    args = ctx.args
    store = CheckinStore(ctx.config)
    if args.delete:
        removed = store.remove(args.delete)
        store.save()
        ctx.echo("Deleted check-in %s from %s"
                 % (removed.id, removed.at.strftime("%Y-%m-%d %H:%M")))
        return 0

    mood = check_score("mood", args.mood)
    energy = check_score("energy", args.energy)
    stress = check_score("stress", args.stress)
    if mood is None and energy is None and stress is None and sys.stdin.isatty():
        mood = check_score("mood", _ask("Mood 1-5 (1 low, 5 good)"))
        energy = check_score("energy", _ask("Energy 1-5"))
        stress = check_score("stress", _ask("Stress 1-5 (1 calm, 5 wound up)"))
    if mood is None and energy is None and stress is None:
        raise MindfulError("give at least one of --mood, --energy or --stress")

    checkin = Checkin(at=resolve_time(ctx, args.at), mood=mood, energy=energy, stress=stress,
                      note=" ".join(args.words).strip(), tags=normalise_tags(args.tag or []))
    store.add(checkin)
    store.save()
    parts = ["%s %d" % (name, value) for name, value in
             (("mood", mood), ("energy", energy), ("stress", stress)) if value is not None]
    ctx.echo("Noted %s at %s [%s]" % (", ".join(parts), checkin.at.strftime("%H:%M"),
                                      ctx.term.paint(checkin.id, "grey")))
    return 0


def _ask(prompt: str):
    try:
        answer = input("%s: " % prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return answer or None


def cmd_mood(ctx: Context) -> int:
    """Show check-ins and how the scales have moved."""
    start, end = ctx.range_of(":month")
    store = CheckinStore(ctx.config)
    checkins = store.select(start, end)
    if getattr(ctx.args, "format", None) == "json":
        print(json.dumps([item.to_dict() for item in checkins], indent=2, ensure_ascii=False))
        return 0
    if not checkins:
        ctx.echo("No check-ins in this range. Add one with `klok checkin --mood 4`.")
        return 1

    rows = [[item.at.strftime("%Y-%m-%d"), ctx.renderer.fmt_time(item.at),
             "" if item.mood is None else str(item.mood),
             "" if item.energy is None else str(item.energy),
             "" if item.stress is None else str(item.stress),
             " ".join("+" + tag for tag in item.tags),
             item.note, ctx.term.paint(item.id, "grey")]
            for item in checkins]
    print(render_table(rows, headers=["Date", "Time", "Mood", "Energy", "Stress", "Tags", "Note", "ID"],
                       aligns=["left", "left", "right", "right", "right", "left", "left", "left"],
                       term=ctx.term))

    print("")
    days = sorted({item.at.date() for item in checkins})
    for field_name in ("mood", "energy", "stress"):
        averages = daily_averages(checkins, field_name)
        if not averages:
            continue
        series = [averages.get(day) for day in days]
        values = [value for value in series if value is not None]
        print("%-8s %s  avg %.1f" % (field_name.title(),
                                     ctx.term.paint(sparkline(series), "cyan"),
                                     sum(values) / len(values)))

    insight = _workload_insight(ctx, checkins, days)
    if insight:
        print("")
        print(ctx.term.paint(insight, "dim"))
    return 0


def _workload_insight(ctx: Context, checkins, days):
    """Compare mood on heavier and lighter days.  Descriptive, not a claim."""
    moods = daily_averages(checkins, "mood")
    if len(moods) < 4:
        return None
    work_sheet = ctx.store.current_sheet
    tracked = {}
    for day in moods:
        day_start = ctx.now.replace(year=day.year, month=day.month, day=day.day,
                                    hour=0, minute=0, second=0, microsecond=0)
        frames = ctx.store.select(day_start, day_start + timedelta(days=1),
                                  sheet=work_sheet, now=ctx.now)
        tracked[day] = sum((frame.duration(ctx.now) for frame in frames), timedelta(0))
    if not any(value.total_seconds() for value in tracked.values()):
        return None
    ordered = sorted(tracked.values())
    median = ordered[len(ordered) // 2]
    heavy = [moods[day] for day in moods if tracked[day] > median]
    light = [moods[day] for day in moods if tracked[day] <= median]
    if len(heavy) < 2 or len(light) < 2:
        return None
    return ("On the %d busier days (over %s tracked) mood averaged %.1f; "
            "on the %d lighter days, %.1f."
            % (len(heavy), format_duration(median), sum(heavy) / len(heavy),
               len(light), sum(light) / len(light)))


def cmd_mindful(ctx: Context) -> int:
    """How the practice itself is going."""
    start, end = ctx.range_of(":month")
    sheet = None if ctx.args.all_sheets else practice_sheet(ctx)
    frames = ctx.store.select(start, end, sheet=sheet, now=ctx.now)
    frames = [frame for frame in frames
              if MEDITATION_TAG in frame.tags or BREATHING_TAG in frame.tags]
    if not frames:
        ctx.echo("No practice recorded in this range. Try `klok meditate 10m` or `klok breathe`.")
        return 1

    renderer = ctx.renderer
    total = sum((renderer.duration_of(frame) for frame in frames), timedelta(0))
    sits = [frame for frame in frames if MEDITATION_TAG in frame.tags]
    breaths = [frame for frame in frames if BREATHING_TAG in frame.tags]
    days = {frame.start.date() for frame in frames}
    last = max(frames, key=lambda frame: frame.end_or(ctx.now))

    rows = [
        ["Range", "%s to %s" % (start.strftime("%Y-%m-%d"),
                                (end - timedelta(seconds=1)).strftime("%Y-%m-%d"))],
        ["Sessions", str(len(frames))],
        ["Total practice", format_duration(total)],
        ["Sitting", "%d session%s, %s" % (len(sits), "" if len(sits) == 1 else "s",
                                          format_duration(sum((renderer.duration_of(f) for f in sits),
                                                              timedelta(0))))],
        ["Breathing", "%d session%s, %s" % (len(breaths), "" if len(breaths) == 1 else "s",
                                            format_duration(sum((renderer.duration_of(f) for f in breaths),
                                                                timedelta(0))))],
        ["Average session", format_duration(total / len(frames))],
        ["Days practised", str(len(days))],
        ["Current streak", "%d day%s" % (streak(days, ctx.now.date()),
                                         "" if streak(days, ctx.now.date()) == 1 else "s")],
        ["Longest streak", "%d day%s" % (longest_streak(days),
                                         "" if longest_streak(days) == 1 else "s")],
        ["Last practice", humanize_ago(ctx.now - last.end_or(ctx.now))],
    ]
    print(render_table(rows, aligns=["left", "left"]))

    if not ctx.args.no_chart:
        print("")
        print(render_day_chart(frames, renderer, start, end))

    checkins = CheckinStore(ctx.config).select(start, end)
    moods = daily_averages(checkins, "mood") if checkins else {}
    if moods:
        series = [moods.get(day) for day in sorted(moods)]
        values = [value for value in series if value is not None]
        print("")
        print("Mood     %s  avg %.1f" % (ctx.term.paint(sparkline(series), "cyan"),
                                         sum(values) / len(values)))
    return 0


# ------------------------------------------------------------------- checks
def cmd_check(ctx: Context) -> int:
    problems = []
    for first, second in ctx.store.overlaps(ctx.now):
        problems.append(("overlap", "%s and %s overlap on sheet %s" % (first.id, second.id, first.sheet)))
    running = [frame for frame in ctx.store.frames if frame.running]
    for frame in running:
        hours = frame.duration(ctx.now).total_seconds() / 3600
        if hours > 12:
            problems.append(("long", "%s has been running for %s" % (frame.id, format_duration(frame.duration(ctx.now)))))
    if len(running) > 1:
        problems.append(("multiple", "%d entries are running at once (%s)" %
                         (len(running), ", ".join(frame.id for frame in running))))
    for frame in ctx.store.frames:
        if frame.stop and frame.stop < frame.start:
            problems.append(("negative", "%s ends before it starts" % frame.id))
        if frame.stop and frame.stop == frame.start:
            problems.append(("empty", "%s has zero duration" % frame.id))
        if frame.start > ctx.now + timedelta(minutes=1):
            problems.append(("future", "%s starts in the future" % frame.id))
    if not problems:
        ctx.echo("%s %d entries, no problems found." % (ctx.term.paint("OK", "green", "bold"),
                                                        len(ctx.store.frames)))
        return 0
    rows = [[ctx.term.paint(kind, "yellow"), message] for kind, message in problems]
    print(render_table(rows, headers=["Issue", "Detail"], aligns=["left", "left"], term=ctx.term))
    return 1


COMPLETIONS = {
    "bash": """# klok bash completion - add to ~/.bashrc:
#   source <(klok completion bash)
_klok_completions() {
  local commands="start stop cancel status restart switch track add stretch edit annotate tag untag
rename move lengthen shorten fill join split delete undo log summary report chart gaps stats projects
tags sheet sheets archive export import config where timer pomodoro breathe meditate checkin mood
mindful check completion version"
  COMPREPLY=($(compgen -W "$commands" -- "${COMP_WORDS[COMP_CWORD]}"))
}
complete -F _klok_completions klok
""",
    "zsh": """# klok zsh completion - add to ~/.zshrc:
#   source <(klok completion zsh)
_klok() {
  local -a commands
  commands=(start stop cancel status restart switch track add stretch edit annotate tag untag rename
move lengthen shorten fill join split delete undo log summary report chart gaps stats projects tags
sheet sheets archive export import config where timer pomodoro breathe meditate checkin mood
mindful check completion version)
  _describe 'klok command' commands
}
compdef _klok klok
""",
    "fish": """# klok fish completion - save as ~/.config/fish/completions/klok.fish
for cmd in start stop cancel status restart switch track add stretch edit annotate tag untag rename \\
    move lengthen shorten fill join split delete undo log summary report chart gaps stats projects \\
    tags sheet sheets archive export import config where timer pomodoro breathe meditate \\
    checkin mood mindful check completion version
    complete -c klok -n __fish_use_subcommand -a $cmd
end
""",
}


def cmd_completion(ctx: Context) -> int:
    print(COMPLETIONS[ctx.args.shell].strip())
    return 0


def cmd_version(ctx: Context) -> int:
    print("klok %s" % __version__)
    return 0


# ------------------------------------------------------------------- parser
GLOBAL_VALUE_OPTIONS = {"--home", "--color", "--round", "--duration-style", "--now"}


def _subcommand_index(argv):
    """Index of the subcommand word, skipping global options and their values."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("-"):
            index += 2 if token in GLOBAL_VALUE_OPTIONS else 1
            continue
        return index
    return None


def _preprocess(argv):
    """Smooth over two things argparse cannot express on its own.

    ``--at -15m`` is folded into ``--at=-15m``, and ``klok tag ID -email``
    keeps working by rewriting the removal marker to ``~`` before argparse
    mistakes it for an option.
    """
    import re

    position = _subcommand_index(argv)
    tag_command = position is not None and argv[position] in ("tag", "untag")
    out, index = [], 0
    while index < len(argv):
        token = argv[index]
        following = argv[index + 1] if index + 1 < len(argv) else None
        if token in TIME_OPTIONS and following and re.match(r"^-\d", following):
            out.append("%s=%s" % (token, following))
            index += 2
            continue
        if (tag_command and index > position and token not in ("-h", "--help", "--sheet")
                and re.match(r"^-[A-Za-z]", token)):
            token = "~" + token[1:]
        out.append(token)
        index += 1
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="klok",
        description="Track where your time goes, from the command line.",
        epilog="Run `klok <command> --help` for the options of a single command.")
    parser.add_argument("--home", metavar="DIR", help="data directory (default: $KLOK_HOME or XDG data dir)")
    parser.add_argument("--color", choices=["auto", "always", "never"], help="colour output")
    parser.add_argument("--no-color", action="store_true", help="disable colour")
    parser.add_argument("--round", type=int, metavar="MIN", help="round durations up to N minutes")
    parser.add_argument("--duration-style", choices=["short", "clock", "hours"], default="short",
                        help="how durations are printed (1h 30m, 01:30:00 or 1.50)")
    parser.add_argument("--now", metavar="TIME", help=argparse.SUPPRESS)
    parser.add_argument("-V", "--version", action="version", version="klok %s" % __version__)

    sheet_opts = argparse.ArgumentParser(add_help=False)
    sheet_opts.add_argument("--sheet", metavar="NAME", help="operate on this sheet instead of the active one")
    sheet_opts.add_argument("--all-sheets", action="store_true", help="include every sheet")

    filter_opts = argparse.ArgumentParser(add_help=False)
    filter_opts.add_argument("-p", "--project", action="append", metavar="NAME",
                             help="only this project (repeatable, globs and parent prefixes work)")
    filter_opts.add_argument("-t", "--tag", action="append", metavar="TAG",
                             help="only entries carrying this tag (repeatable, all must match)")
    filter_opts.add_argument("-T", "--exclude-tag", action="append", metavar="TAG",
                             help="skip entries carrying this tag (repeatable)")
    filter_opts.add_argument("--contains", metavar="TEXT", help="only entries whose text contains TEXT")

    range_opts = argparse.ArgumentParser(add_help=False)
    range_opts.add_argument("range_tokens", nargs="*", metavar="RANGE",
                            help="e.g. :week, yesterday, 'last 7 days', '2026-08-01 to 2026-08-31'")
    range_opts.add_argument("--from", dest="from_time", metavar="TIME", help="range start")
    range_opts.add_argument("--to", dest="to_time", metavar="TIME", help="range end")

    format_opts = argparse.ArgumentParser(add_help=False)
    format_opts.add_argument("-f", "--format", choices=list(FORMATS), help="output format (default: text)")

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name, help_text, aliases=(), parents=(), func=None, **kwargs):
        sub = subparsers.add_parser(name, help=help_text, aliases=list(aliases),
                                    parents=list(parents), description=help_text, **kwargs)
        sub.set_defaults(func=func, command=name)
        return sub

    # -- tracking ------------------------------------------------------
    start = add("start", "Start tracking a project now (or at a given time).",
                aliases=["in", "on"], parents=[sheet_opts], func=cmd_start)
    start.add_argument("words", nargs="*", metavar="PROJECT +TAG", help="project name and +tags")
    start.add_argument("-a", "--at", metavar="TIME", help="start at this time (e.g. 9am, -15m)")
    start.add_argument("-n", "--note", metavar="TEXT", help="annotation for the entry")
    start.add_argument("--no-stop", action="store_true", help="fail instead of stopping what is running")
    start.add_argument("--force", action="store_true", help="allow a project that has never been used")

    stop = add("stop", "Stop the running entry.", aliases=["out", "off"],
               parents=[sheet_opts], func=cmd_stop)
    stop.add_argument("-a", "--at", metavar="TIME", help="stop at this time (e.g. 17:30, -5m)")
    stop.add_argument("-n", "--note", metavar="TEXT", help="append an annotation while stopping")
    stop.add_argument("--all", action="store_true", help="stop running entries on every sheet")

    add("cancel", "Discard the running entry without recording it.",
        aliases=["abort"], parents=[sheet_opts], func=cmd_cancel)

    status = add("status", "Show what is running right now.", aliases=["now", "current"],
                 parents=[sheet_opts], func=cmd_status)
    status.add_argument("--json", dest="format", action="store_const", const="json",
                        help="machine-readable output")
    status.add_argument("-q", "--quiet", action="store_true", help="say nothing when idle")

    restart = add("restart", "Start a new entry with a previous entry's project and tags.",
                  aliases=["continue", "resume", "again"], parents=[sheet_opts], func=cmd_restart)
    restart.add_argument("ref", nargs="?", metavar="ID", help="entry to copy (default: the last one)")
    restart.add_argument("-a", "--at", metavar="TIME", help="start at this time")
    restart.add_argument("--keep-note", action="store_true", help="carry the annotation over too")

    switch = add("switch", "Stop what is running and start something else at the same instant.",
                 parents=[sheet_opts], func=cmd_switch)
    switch.add_argument("words", nargs="*", metavar="PROJECT +TAG")
    switch.add_argument("-a", "--at", metavar="TIME", help="switch at this time")
    switch.add_argument("-n", "--note", metavar="TEXT")

    track = add("track", "Record a finished interval, e.g. `klok track 09:00 to 11:30 acme +api`.",
                parents=[sheet_opts], func=cmd_track)
    track.add_argument("words", nargs="*", metavar="RANGE PROJECT +TAG")
    track.add_argument("--from", dest="from_time", metavar="TIME")
    track.add_argument("--to", dest="to_time", metavar="TIME")
    track.add_argument("-d", "--duration", metavar="DURATION", help="length instead of an end time")
    track.add_argument("-n", "--note", metavar="TEXT")
    track.add_argument("--force", action="store_true")

    entry_add = add("add", "Close the open stretch since the last entry with an activity (utt style).",
                    parents=[sheet_opts], func=cmd_add)
    entry_add.add_argument("words", nargs="*", metavar="PROJECT +TAG")
    entry_add.add_argument("-a", "--at", metavar="TIME", help="end the activity at this time")
    entry_add.add_argument("-n", "--note", metavar="TEXT")
    entry_add.add_argument("--force", action="store_true")

    stretch = add("stretch", "Pull an entry's start back to the end of the previous one.",
                  parents=[sheet_opts], func=cmd_stretch)
    stretch.add_argument("ref", nargs="?", metavar="ID")
    stretch.add_argument("--by", metavar="DURATION", help="stretch by a fixed amount instead")

    # -- editing -------------------------------------------------------
    edit = add("edit", "Edit entries in $EDITOR, or change fields directly with flags.",
               parents=[sheet_opts], func=cmd_edit)
    edit.add_argument("ref", nargs="?", metavar="ID", help="entry to edit (default: running or last)")
    edit.add_argument("--range", dest="range_tokens", nargs="*", default=[], metavar="RANGE",
                      help="edit every entry in a range")
    edit.add_argument("--all", action="store_true", help="edit every entry on the sheet")
    edit.add_argument("--start", metavar="TIME")
    edit.add_argument("--stop", metavar="TIME", help="new end time, or 'running' to reopen")
    edit.add_argument("--set-project", dest="set_project", metavar="NAME", help="replace the project")
    edit.add_argument("-n", "--note", metavar="TEXT")
    edit.add_argument("--tags", dest="set_tags", nargs="*", metavar="TAG", help="replace the tag list")
    edit.set_defaults(project=None, tag=None, exclude_tag=None, contains=None)

    annotate = add("annotate", "Attach a note to an entry.", aliases=["note"], func=cmd_annotate)
    annotate.add_argument("ref", nargs="?", metavar="ID")
    annotate.add_argument("words", nargs="*", metavar="TEXT")
    annotate.add_argument("--append", action="store_true", help="append instead of replacing")
    annotate.add_argument("--sheet", metavar="NAME")
    annotate.set_defaults(all_sheets=False)

    tag = add("tag", "Add (+tag) or remove (-tag) tags on an entry.", func=cmd_tag)
    tag.add_argument("ref", metavar="ID")
    tag.add_argument("words", nargs="+", metavar="+TAG|-TAG",
                     help="+name adds a tag, -name removes one")
    tag.add_argument("--sheet", metavar="NAME")
    tag.set_defaults(all_sheets=False)

    untag = add("untag", "Remove tags from an entry.", func=cmd_untag)
    untag.add_argument("ref", metavar="ID")
    untag.add_argument("words", nargs="+", metavar="TAG")
    untag.add_argument("--sheet", metavar="NAME")
    untag.set_defaults(all_sheets=False)

    rename = add("rename", "Rename a project, tag or sheet everywhere.", func=cmd_rename)
    rename.add_argument("kind", choices=["project", "tag", "sheet"])
    rename.add_argument("old")
    rename.add_argument("new")

    move = add("move", "Move an entry to a new start time, keeping its length.",
               parents=[sheet_opts], func=cmd_move)
    move.add_argument("ref", metavar="ID")
    move.add_argument("time", metavar="TIME")

    for name, help_text in (("lengthen", "Make an entry longer."), ("shorten", "Make an entry shorter.")):
        resize = add(name, help_text, parents=[sheet_opts], func=cmd_resize)
        resize.add_argument("ref", metavar="ID")
        resize.add_argument("duration", metavar="DURATION")
        resize.add_argument("--start-side", action="store_true",
                            help="move the start instead of the end")

    fill = add("fill", "Expand an entry until it touches its neighbours.",
               parents=[sheet_opts], func=cmd_fill)
    fill.add_argument("ref", metavar="ID")

    join = add("join", "Merge two or more entries into one.", parents=[sheet_opts], func=cmd_join)
    join.add_argument("refs", nargs="+", metavar="ID")

    split = add("split", "Split an entry in two, or into N equal pieces.",
                parents=[sheet_opts], func=cmd_split)
    split.add_argument("ref", metavar="ID")
    split.add_argument("--at", metavar="TIME", help="split at this time")
    split.add_argument("--into", type=int, default=2, metavar="N", help="split into N equal pieces")

    delete = add("delete", "Delete entries (undoable).", aliases=["rm", "remove", "kill"],
                 parents=[sheet_opts], func=cmd_delete)
    delete.add_argument("refs", nargs="+", metavar="ID")

    add("undo", "Undo the last change to the timesheet.", func=cmd_undo)

    # -- reporting -----------------------------------------------------
    log = add("log", "List entries day by day.", aliases=["display", "list"],
              parents=[range_opts, filter_opts, sheet_opts, format_opts], func=cmd_log)
    log.add_argument("--limit", type=int, metavar="N", help="only the last N entries")
    log.add_argument("--reverse", action="store_true", help="newest first")
    log.add_argument("--no-notes", action="store_true", help="hide annotations")

    add("summary", "A dense table of entries with daily subtotals.",
        parents=[range_opts, filter_opts, sheet_opts, format_opts], func=cmd_summary)

    report = add("report", "Totals per project, broken down by tag.",
                 parents=[range_opts, filter_opts, sheet_opts, format_opts], func=cmd_report)
    report.add_argument("--entries", action="store_true", help="also list the individual entries")
    report.add_argument("--no-tags", action="store_true", help="skip the tag breakdown")

    chart = add("chart", "Bar charts and a punchcard of when you work.",
                parents=[range_opts, filter_opts, sheet_opts], func=cmd_chart)
    chart.add_argument("--by", choices=["day", "project", "tag", "sheet", "punchcard"],
                       default="day", help="what to chart (default: day)")

    gaps = add("gaps", "Show untracked stretches of time.",
               parents=[range_opts, sheet_opts], func=cmd_gaps)
    gaps.add_argument("--min", metavar="DURATION", help="ignore gaps shorter than this (default 5m)")

    add("stats", "A quick numeric overview of a range.",
        parents=[range_opts, filter_opts, sheet_opts], func=cmd_stats)

    projects = add("projects", "List projects with their totals.",
                   parents=[range_opts, filter_opts, sheet_opts], func=cmd_projects)
    projects.add_argument("-q", "--quiet", action="store_true", help="names only, one per line")

    tags_cmd = add("tags", "List tags with their totals.",
                   parents=[range_opts, filter_opts, sheet_opts], func=cmd_tags)
    tags_cmd.add_argument("-q", "--quiet", action="store_true", help="names only, one per line")

    export = add("export", "Export entries as JSON, CSV, iCal, ledger or Markdown.",
                 parents=[range_opts, filter_opts, sheet_opts, format_opts], func=cmd_export)
    export.add_argument("-o", "--output", metavar="FILE", help="write to a file instead of stdout")

    import_cmd = add("import", "Import entries from JSON, JSONL or CSV.", func=cmd_import)
    import_cmd.add_argument("file", nargs="?", metavar="FILE", help="file to read (default: stdin)")
    import_cmd.add_argument("--sheet", metavar="NAME", help="put everything on this sheet")
    import_cmd.set_defaults(all_sheets=False)

    # -- sheets and config ---------------------------------------------
    sheet_cmd = add("sheet", "Show or switch the active sheet ('-' goes back).", func=cmd_sheet)
    sheet_cmd.add_argument("name", nargs="?", metavar="NAME")

    add("sheets", "List sheets with their totals.", func=cmd_sheets)

    add("archive", "Move entries to an archived sheet so they stay out of reports.",
        parents=[range_opts, filter_opts, sheet_opts], func=cmd_archive)

    config_cmd = add("config", "Read or change configuration.", func=cmd_config)
    config_cmd.add_argument("action", nargs="?", choices=["get", "set", "unset", "list", "edit", "path"],
                            default="list")
    config_cmd.add_argument("key", nargs="?", metavar="SECTION.OPTION")
    config_cmd.add_argument("value", nargs="?")

    add("where", "Print the paths klok reads and writes.", aliases=["paths"], func=cmd_where)

    # -- focus ---------------------------------------------------------
    timer = add("timer", "Run a countdown timer with a notification at the end.",
                parents=[sheet_opts], func=cmd_timer)
    timer.add_argument("duration", metavar="DURATION", help="e.g. 25m, 1h30m, 90")
    timer.add_argument("-m", "--message", metavar="TEXT", help="label shown while counting down")
    timer.add_argument("--art", choices=sorted(ART), help="ASCII art to sit above the clock")
    timer.add_argument("--big", action="store_true", help="draw the remaining time in block digits")
    timer.add_argument("-q", "--quiet", action="store_true", help="no live display")
    timer.add_argument("-p", "--project", metavar="NAME", help="also record the time against a project")
    timer.add_argument("-t", "--tag", action="append", metavar="TAG")
    timer.add_argument("--no-track", action="store_true", help="never record a frame")

    pomo = add("pomodoro", "Run pomodoro rounds and record the focus time.", aliases=["pomo", "focus"],
               parents=[sheet_opts], func=cmd_pomodoro)
    pomo.add_argument("words", nargs="*", metavar="PROJECT +TAG")
    pomo.add_argument("--work", metavar="DURATION")
    pomo.add_argument("--break", dest="short_break", metavar="DURATION")
    pomo.add_argument("--long-break", dest="long_break", metavar="DURATION")
    pomo.add_argument("--rounds", type=int, metavar="N")
    pomo.add_argument("--no-breaks", action="store_true", help="work rounds only")
    pomo.add_argument("--no-prompt", action="store_true", help="do not wait for enter between rounds")
    pomo.add_argument("--no-track", action="store_true", help="do not record frames")
    pomo.add_argument("--art", choices=sorted(ART))
    pomo.add_argument("--big", action="store_true")
    pomo.add_argument("-q", "--quiet", action="store_true")

    # -- mindfulness ---------------------------------------------------
    breathe_cmd = add("breathe", "Run a guided breathing pacer.", aliases=["breath"],
                      parents=[sheet_opts], func=cmd_breathe)
    breathe_cmd.add_argument("words", nargs="*", metavar="PATTERN",
                             help="a named pattern (box, calm, relax, ...) or counts like 4-7-8")
    breathe_cmd.add_argument("--pattern", metavar="NAME|COUNTS", help="same, as a flag")
    breathe_cmd.add_argument("--rounds", type=int, metavar="N", help="number of breaths")
    breathe_cmd.add_argument("--for", dest="for_time", metavar="DURATION",
                             help="breathe for this long instead of a round count")
    breathe_cmd.add_argument("--list", action="store_true", help="list the named patterns")
    breathe_cmd.add_argument("--plan", action="store_true", help="describe the session without running it")
    breathe_cmd.add_argument("-q", "--quiet", action="store_true", help="no live display")
    breathe_cmd.add_argument("-n", "--note", metavar="TEXT")
    breathe_cmd.add_argument("-t", "--tag", action="append", metavar="TAG")
    breathe_cmd.add_argument("--no-track", action="store_true", help="do not record the session")

    meditate_cmd = add("meditate", "Sit for a set time, with a bell at the end.",
                       aliases=["sit", "zen"], parents=[sheet_opts], func=cmd_meditate)
    meditate_cmd.add_argument("duration", nargs="?", metavar="DURATION",
                              help="how long to sit (default: mindful.default_sit)")
    meditate_cmd.add_argument("--for", dest="for_time", metavar="DURATION")
    meditate_cmd.add_argument("--interval-bell", metavar="DURATION", default=None,
                              help="ring a bell this often during the sit")
    meditate_cmd.add_argument("--warmup", metavar="DURATION", default=None,
                              help="a settling bell this far in")
    meditate_cmd.add_argument("--no-guidance", action="store_true", help="no prompts, just the bells")
    meditate_cmd.add_argument("--plan", action="store_true", help="describe the sit without running it")
    meditate_cmd.add_argument("-q", "--quiet", action="store_true")
    meditate_cmd.add_argument("-n", "--note", metavar="TEXT")
    meditate_cmd.add_argument("-t", "--tag", action="append", metavar="TAG")
    meditate_cmd.add_argument("--no-track", action="store_true")

    checkin_cmd = add("checkin", "Note how you are doing on three 1-5 scales.",
                      aliases=["feel"], func=cmd_checkin)
    checkin_cmd.add_argument("words", nargs="*", metavar="NOTE")
    checkin_cmd.add_argument("--mood", metavar="1-5")
    checkin_cmd.add_argument("--energy", metavar="1-5")
    checkin_cmd.add_argument("--stress", metavar="1-5")
    checkin_cmd.add_argument("-a", "--at", metavar="TIME")
    checkin_cmd.add_argument("-t", "--tag", action="append", metavar="TAG")
    checkin_cmd.add_argument("--delete", metavar="ID", help="remove a check-in")

    mood_cmd = add("mood", "Show check-ins and how the scales have moved.",
                   parents=[range_opts], func=cmd_mood)
    mood_cmd.add_argument("--json", dest="format", action="store_const", const="json")

    mindful_cmd = add("mindful", "How the practice itself is going.", aliases=["practice"],
                      parents=[range_opts, sheet_opts], func=cmd_mindful)
    mindful_cmd.add_argument("--no-chart", action="store_true", help="skip the per-day chart")

    add("check", "Look for overlaps and other suspicious entries.", aliases=["sanity", "doctor"],
        func=cmd_check)

    completion = add("completion", "Print a shell completion script.", func=cmd_completion)
    completion.add_argument("shell", choices=sorted(COMPLETIONS))

    add("version", "Print the version.", func=cmd_version)
    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["status"]
    parser = build_parser()
    args = parser.parse_args(_preprocess(argv))
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        ctx = Context(args)
        return args.func(ctx) or 0
    except (StoreError, TimeParseError, ValueError, KeyError) as exc:
        return die(str(exc))
    except FileNotFoundError as exc:
        return die("%s" % exc)
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
