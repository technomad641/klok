"""Aggregation and the human-readable renderings built on top of it."""

from collections import OrderedDict, defaultdict
from datetime import date, datetime, timedelta

from klok.timeparse import (format_datetime, format_duration, format_time,
                            round_duration, week_bounds)
from klok.utils import Term, bar, render_table


class Renderer:
    """Shared presentation settings for every report style."""

    def __init__(self, term: Term, now: datetime, time_format: str = "24",
                 round_minutes: int = 0, week_start: str = "monday",
                 duration_style: str = "short"):
        self.term = term
        self.now = now
        self.time_format = time_format
        self.round_minutes = round_minutes
        self.week_start = week_start
        self.duration_style = duration_style

    # -- helpers -------------------------------------------------------
    def duration_of(self, frame) -> timedelta:
        return round_duration(frame.duration(self.now), self.round_minutes)

    def fmt_duration(self, delta: timedelta) -> str:
        return format_duration(delta, self.duration_style)

    def fmt_time(self, value: datetime) -> str:
        return format_time(value, self.time_format)

    def project_cell(self, frame) -> str:
        name = frame.project or "(no project)"
        return self.term.paint(name, self.term.project_color(frame.project))

    def tag_cell(self, frame) -> str:
        if not frame.tags:
            return ""
        return self.term.paint(" ".join("+" + tag for tag in frame.tags), "blue")

    def clipped(self, frame, start: datetime, end: datetime) -> timedelta:
        """Duration of the part of ``frame`` that falls inside a range."""
        frame_end = frame.end_or(self.now)
        span = min(frame_end, end) - max(frame.start, start)
        if span.total_seconds() <= 0:
            return timedelta(0)
        return round_duration(span, self.round_minutes)


# ------------------------------------------------------------- aggregation
def total(frames, renderer: Renderer) -> timedelta:
    return sum((renderer.duration_of(frame) for frame in frames), timedelta(0))


def group_by(frames, key, renderer: Renderer) -> "OrderedDict[str, timedelta]":
    buckets = defaultdict(timedelta)
    for frame in frames:
        for value in key(frame):
            buckets[value] += renderer.duration_of(frame)
    return OrderedDict(sorted(buckets.items(), key=lambda item: (-item[1], item[0])))


def by_project(frames, renderer: Renderer):
    return group_by(frames, lambda frame: [frame.project or "(no project)"], renderer)


def by_tag(frames, renderer: Renderer):
    return group_by(frames, lambda frame: frame.tags or ["(untagged)"], renderer)


def by_day(frames, renderer: Renderer) -> "OrderedDict[date, timedelta]":
    buckets = defaultdict(timedelta)
    for frame in frames:
        buckets[frame.start.date()] += renderer.duration_of(frame)
    return OrderedDict(sorted(buckets.items()))


def by_sheet(frames, renderer: Renderer):
    return group_by(frames, lambda frame: [frame.sheet], renderer)


# ---------------------------------------------------------------- renderers
def render_log(frames, renderer: Renderer, show_notes: bool = True) -> str:
    """Chronological entries grouped by day, newest day last."""
    if not frames:
        return "No time tracked in this range."
    days = defaultdict(list)
    for frame in frames:
        days[frame.start.date()].append(frame)

    blocks = []
    for day in sorted(days):
        entries = sorted(days[day], key=lambda frame: frame.start)
        day_total = sum((renderer.duration_of(frame) for frame in entries), timedelta(0))
        heading = "%s  %s" % (day.strftime("%a %d %B %Y"), renderer.fmt_duration(day_total))
        rows, aligns = [], ["left", "left", "left", "left", "right"]
        for frame in entries:
            when = "%s to %s" % (renderer.fmt_time(frame.start),
                                 renderer.fmt_time(frame.stop) if frame.stop else "now")
            note = frame.note if show_notes else ""
            rows.append([
                renderer.term.paint(frame.id, "grey"),
                when,
                renderer.project_cell(frame),
                " ".join(filter(None, [renderer.tag_cell(frame),
                                       renderer.term.paint(note, "dim") if note else ""])),
                renderer.fmt_duration(renderer.duration_of(frame)),
            ])
        blocks.append(renderer.term.paint(heading, "bold") + "\n" +
                      render_table(rows, aligns=aligns, indent="  "))
    grand = sum((renderer.duration_of(frame) for frame in frames), timedelta(0))
    blocks.append(renderer.term.paint("Total: %s" % renderer.fmt_duration(grand), "bold"))
    return "\n\n".join(blocks)


def render_summary(frames, renderer: Renderer, start: datetime = None, end: datetime = None) -> str:
    """A dense table with per-day subtotals, in the spirit of Timewarrior."""
    if not frames:
        return "No time tracked in this range."
    headers = ["Wk", "Date", "Day", "ID", "Project", "Tags", "Start", "End", "Time", "Total"]
    aligns = ["left", "left", "left", "left", "left", "left", "left", "left", "right", "right"]
    rows = []
    days = defaultdict(list)
    for frame in frames:
        days[frame.start.date()].append(frame)

    grand = timedelta(0)
    for day in sorted(days):
        entries = sorted(days[day], key=lambda frame: frame.start)
        day_total = timedelta(0)
        week = "W%02d" % day.isocalendar()[1]
        for index, frame in enumerate(entries):
            length = renderer.duration_of(frame)
            day_total += length
            rows.append([
                week if index == 0 else "",
                day.isoformat() if index == 0 else "",
                day.strftime("%a") if index == 0 else "",
                renderer.term.paint(frame.id, "grey"),
                renderer.project_cell(frame),
                renderer.tag_cell(frame),
                renderer.fmt_time(frame.start),
                renderer.fmt_time(frame.stop) if frame.stop else "now",
                renderer.fmt_duration(length),
                "",
            ])
        rows[-1][-1] = renderer.term.paint(renderer.fmt_duration(day_total), "bold")
        grand += day_total
    table = render_table(rows, headers=headers, aligns=aligns, term=renderer.term)
    return "%s\n\n%s" % (table, renderer.term.paint("Total: %s" % renderer.fmt_duration(grand), "bold"))


def render_report(frames, renderer: Renderer, start: datetime, end: datetime,
                  show_tags: bool = True, show_entries: bool = False) -> str:
    """Totals per project, with an optional tag and entry breakdown."""
    if not frames:
        return "No time tracked in this range."
    header = "%s -> %s" % (start.strftime("%a %d %B %Y"), (end - timedelta(seconds=1)).strftime("%a %d %B %Y"))
    lines = [renderer.term.paint(header, "bold"), ""]

    grouped = defaultdict(list)
    for frame in frames:
        grouped[frame.project or "(no project)"].append(frame)

    ordered = sorted(grouped.items(),
                     key=lambda item: -sum((renderer.duration_of(f) for f in item[1]), timedelta(0)))
    grand = timedelta(0)
    for project, entries in ordered:
        project_total = sum((renderer.duration_of(frame) for frame in entries), timedelta(0))
        grand += project_total
        colour = renderer.term.project_color(entries[0].project)
        lines.append("%s - %s" % (renderer.term.paint(project, colour, "bold"),
                                  renderer.fmt_duration(project_total)))
        if show_tags:
            tag_totals = by_tag(entries, renderer)
            rows = [[renderer.term.paint("+" + name if name != "(untagged)" else name, "blue"),
                     renderer.fmt_duration(length)]
                    for name, length in tag_totals.items()]
            if rows:
                lines.append(render_table(rows, aligns=["left", "right"], indent="        "))
        if show_entries:
            rows = [[renderer.term.paint(frame.id, "grey"),
                     format_datetime(frame.start, renderer.time_format),
                     renderer.fmt_duration(renderer.duration_of(frame)),
                     renderer.term.paint(frame.note, "dim")]
                    for frame in sorted(entries, key=lambda f: f.start)]
            lines.append(render_table(rows, aligns=["left", "left", "right", "left"], indent="        "))
        lines.append("")
    lines.append(renderer.term.paint("Total: %s" % renderer.fmt_duration(grand), "bold"))
    return "\n".join(lines)


def render_totals(totals, renderer: Renderer, title: str = "", width: int = 28) -> str:
    """A labelled bar chart for any ``label -> duration`` mapping."""
    if not totals:
        return "No time tracked in this range."
    biggest = max(totals.values())
    grand = sum(totals.values(), timedelta(0))
    rows = []
    for label, length in totals.items():
        share = (length.total_seconds() / grand.total_seconds() * 100) if grand else 0
        rows.append([
            str(label),
            renderer.term.paint(bar(length.total_seconds(), biggest.total_seconds(), width), "cyan"),
            renderer.fmt_duration(length),
            "%5.1f%%" % share,
        ])
    table = render_table(rows, aligns=["left", "left", "right", "right"])
    head = renderer.term.paint(title, "bold") + "\n" if title else ""
    return "%s%s\n\n%s" % (head, table, renderer.term.paint("Total: %s" % renderer.fmt_duration(grand), "bold"))


def render_day_chart(frames, renderer: Renderer, start: datetime, end: datetime, width: int = 34) -> str:
    """One bar per calendar day across the whole range, including empty days."""
    totals = OrderedDict()
    cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
    limit = end
    while cursor < limit:
        next_day = cursor + timedelta(days=1)
        amount = sum((renderer.clipped(frame, cursor, next_day) for frame in frames), timedelta(0))
        totals[cursor.date()] = amount
        cursor = next_day
        if len(totals) > 400:
            break
    if not totals:
        return "No time tracked in this range."
    biggest = max(totals.values()) or timedelta(seconds=1)
    rows = []
    for day, amount in totals.items():
        label = "%s %s" % (day.isoformat(), day.strftime("%a"))
        weekend = day.weekday() >= 5
        rows.append([
            renderer.term.paint(label, "grey") if weekend else label,
            renderer.term.paint(bar(amount.total_seconds(), biggest.total_seconds(), width), "cyan"),
            renderer.fmt_duration(amount) if amount else renderer.term.paint("-", "grey"),
        ])
    grand = sum(totals.values(), timedelta(0))
    tracked_days = len([value for value in totals.values() if value])
    average = grand / tracked_days if tracked_days else timedelta(0)
    footer = "Total: %s over %d day%s (avg %s per tracked day)" % (
        renderer.fmt_duration(grand), tracked_days, "" if tracked_days == 1 else "s",
        renderer.fmt_duration(average))
    return "%s\n\n%s" % (render_table(rows, aligns=["left", "left", "right"]),
                         renderer.term.paint(footer, "bold"))


def render_punchcard(frames, renderer: Renderer) -> str:
    """A weekday x hour heat map of when the work actually happens."""
    grid = [[timedelta(0)] * 24 for _ in range(7)]
    for frame in frames:
        cursor = frame.start
        end = frame.end_or(renderer.now)
        while cursor < end:
            slot_end = min(end, (cursor + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0))
            if slot_end <= cursor:
                slot_end = cursor + timedelta(hours=1)
            grid[cursor.weekday()][cursor.hour] += min(slot_end, end) - cursor
            cursor = slot_end
    peak = max((cell for row in grid for cell in row), default=timedelta(0))
    if not peak:
        return "No time tracked in this range."
    shades = " .:-=+*#%@"
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    ruler = [" "] * 24
    for hour in range(0, 24, 3):
        for offset, char in enumerate(str(hour)):
            if hour + offset < 24:
                ruler[hour + offset] = char
    lines = [renderer.term.paint("     " + "".join(ruler) + "   Total", "bold")]
    for index, row in enumerate(grid):
        cells = []
        for cell in row:
            level = int(round((cell.total_seconds() / peak.total_seconds()) * (len(shades) - 1)))
            cells.append(shades[level])
        day_total = sum(row, timedelta(0))
        lines.append("%-4s %s  %s" % (names[index], renderer.term.paint("".join(cells), "cyan"),
                                      renderer.fmt_duration(day_total) if day_total else ""))
    lines.append("")
    lines.append(renderer.term.paint(
        "Each column is an hour of the day; darker means more time. Busiest hour holds %s."
        % renderer.fmt_duration(peak), "dim"))
    return "\n".join(lines)


def render_gaps(gaps, renderer: Renderer) -> str:
    if not gaps:
        return "No untracked gaps in this range."
    rows = []
    total_gap = timedelta(0)
    for start, end in gaps:
        length = end - start
        total_gap += length
        rows.append([
            start.strftime("%Y-%m-%d %a"),
            renderer.fmt_time(start),
            renderer.fmt_time(end),
            renderer.fmt_duration(length),
        ])
    table = render_table(rows, headers=["Date", "From", "To", "Gap"],
                         aligns=["left", "left", "left", "right"], term=renderer.term)
    return "%s\n\n%s" % (table, renderer.term.paint("Untracked: %s" % renderer.fmt_duration(total_gap), "bold"))


def week_label(value: datetime, week_start: str) -> str:
    start, end = week_bounds(value, week_start)
    return "%s - %s" % (start.strftime("%Y-%m-%d"), (end - timedelta(days=1)).strftime("%Y-%m-%d"))
