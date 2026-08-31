"""Machine-readable output: JSON, CSV, iCalendar, ledger timeclock, Markdown.

These are what let klok hand its data to a spreadsheet, an invoicing
script or ``hledger`` without anyone writing a parser.
"""

import csv
import io
import json
from datetime import timedelta

from klok.model import Frame
from klok.timeparse import format_duration, to_iso

FORMATS = ("text", "json", "csv", "ical", "ledger", "timeclock", "md", "tsv")


def _rows(frames, now):
    for frame in frames:
        end = frame.end_or(now)
        yield {
            "id": frame.id,
            "sheet": frame.sheet,
            "project": frame.project,
            "tags": frame.tags,
            "note": frame.note,
            "start": to_iso(frame.start),
            "stop": to_iso(frame.stop) if frame.stop else "",
            "running": frame.running,
            "seconds": int((end - frame.start).total_seconds()),
            "hours": round((end - frame.start).total_seconds() / 3600.0, 4),
        }


def to_json(frames, now, indent: int = 2) -> str:
    return json.dumps(list(_rows(frames, now)), indent=indent, ensure_ascii=False)


def to_csv(frames, now, delimiter: str = ",") -> str:
    buffer = io.StringIO()
    fields = ["id", "sheet", "project", "tags", "note", "start", "stop", "seconds", "hours"]
    writer = csv.DictWriter(buffer, fieldnames=fields, delimiter=delimiter, lineterminator="\n")
    writer.writeheader()
    for row in _rows(frames, now):
        row = dict(row)
        row["tags"] = " ".join(row["tags"])
        row.pop("running", None)
        writer.writerow(row)
    return buffer.getvalue().rstrip("\n")


def to_ical(frames, now) -> str:
    def stamp(value):
        return value.astimezone().strftime("%Y%m%dT%H%M%S")

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//klok//EN", "CALSCALE:GREGORIAN"]
    for frame in frames:
        summary = frame.project or "(no project)"
        if frame.tags:
            summary += " " + " ".join("+" + tag for tag in frame.tags)
        lines += [
            "BEGIN:VEVENT",
            "UID:%s@klok" % frame.id,
            "DTSTART:%s" % stamp(frame.start),
            "DTEND:%s" % stamp(frame.end_or(now)),
            "SUMMARY:%s" % _escape_ical(summary),
        ]
        if frame.note:
            lines.append("DESCRIPTION:%s" % _escape_ical(frame.note))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\n".join(lines)


def _escape_ical(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,").replace("\n", "\\n")


def to_timeclock(frames, now) -> str:
    """The ``i``/``o`` timeclock format ledger and hledger read natively."""
    lines = []
    for frame in frames:
        account = frame.project or "unsorted"
        if frame.tags:
            account += ":" + ":".join(frame.tags)
        lines.append("i %s %s%s" % (frame.start.strftime("%Y/%m/%d %H:%M:%S"), account,
                                    ("  " + frame.note) if frame.note else ""))
        lines.append("o %s" % frame.end_or(now).strftime("%Y/%m/%d %H:%M:%S"))
    return "\n".join(lines)


def to_ledger(frames, now) -> str:
    """Postings with hour amounts, one transaction per entry."""
    lines = []
    for frame in frames:
        hours = (frame.end_or(now) - frame.start).total_seconds() / 3600.0
        payee = frame.note or frame.project or "time"
        lines.append("%s * %s" % (frame.start.strftime("%Y/%m/%d"), payee))
        account = (frame.project or "unsorted").replace(".", ":")
        lines.append("    %s  %.2fh" % (account, hours))
        lines.append("    (%s)" % frame.sheet)
        lines.append("")
    return "\n".join(lines).rstrip()


def to_markdown(frames, now) -> str:
    lines = ["| Date | Start | End | Project | Tags | Note | Duration |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    total = timedelta(0)
    for frame in frames:
        length = frame.end_or(now) - frame.start
        total += length
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            frame.start.strftime("%Y-%m-%d"),
            frame.start.strftime("%H:%M"),
            frame.stop.strftime("%H:%M") if frame.stop else "running",
            frame.project or "",
            " ".join("+" + tag for tag in frame.tags),
            frame.note.replace("|", "\\|"),
            format_duration(length),
        ))
    lines.append("")
    lines.append("**Total: %s**" % format_duration(total))
    return "\n".join(lines)


def render(frames, now, fmt: str) -> str:
    fmt = (fmt or "json").lower()
    if fmt == "json":
        return to_json(frames, now)
    if fmt == "csv":
        return to_csv(frames, now)
    if fmt == "tsv":
        return to_csv(frames, now, delimiter="\t")
    if fmt == "ical":
        return to_ical(frames, now)
    if fmt == "timeclock":
        return to_timeclock(frames, now)
    if fmt == "ledger":
        return to_ledger(frames, now)
    if fmt in ("md", "markdown"):
        return to_markdown(frames, now)
    raise ValueError("unknown format %r (pick one of %s)" % (fmt, ", ".join(FORMATS)))


def parse_import(text: str):
    """Read frames from JSON, JSONL or CSV produced by klok or a sibling tool."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return [Frame.from_dict(item) for item in json.loads(text)]
    if text.startswith("{"):
        return [Frame.from_dict(json.loads(line)) for line in text.splitlines() if line.strip()]
    reader = csv.DictReader(io.StringIO(text))
    frames = []
    for row in reader:
        if not row.get("start"):
            continue
        frames.append(Frame.from_dict({
            "id": row.get("id"),
            "start": row["start"],
            "stop": row.get("stop") or row.get("end") or None,
            "project": row.get("project", ""),
            "tags": row.get("tags", ""),
            "note": row.get("note", ""),
            "sheet": row.get("sheet") or "default",
        }))
    return frames
