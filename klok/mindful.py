"""Breathing pacers, sitting timers and mood check-ins.

Practice is recorded as ordinary frames - on their own sheet by default,
so a week of sitting never lands in a client's invoice - which means
`klok report` and friends work on it unchanged.  Mood check-ins are
points in time rather than intervals, so they get a small store of their
own next to the frames file.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from klok.focus import notify
from klok.timeparse import format_duration, from_iso, to_iso
from klok.utils import bar

MEDITATION_TAG = "meditation"
BREATHING_TAG = "breathing"

# name -> (inhale, hold, exhale, hold-out, one-line description)
PATTERNS = {
    "box": (4, 4, 4, 4, "Equal counts all round - steady and easy to hold on to."),
    "calm": (4, 0, 6, 0, "A longer out-breath than in-breath."),
    "coherent": (5.5, 0, 5.5, 0, "About five and a half breaths a minute, evenly."),
    "relax": (4, 7, 8, 0, "The 4-7-8 count, with a long exhale."),
    "triangle": (4, 4, 4, 0, "Inhale, hold, exhale - no pause at the bottom."),
    "even": (5, 0, 5, 0, "Plain, symmetrical breathing."),
}

PHASE_LABELS = [("inhale", "Breathe in"), ("hold", "Hold"),
                ("exhale", "Breathe out"), ("hold_out", "Rest")]

GUIDANCE_START = [
    "Settle in. Let the eyes close or rest on one spot.",
    "Nothing to solve for the next {length}.",
]
GUIDANCE_INTERVAL = [
    "Notice where the attention went, and walk it back.",
    "Feel the weight of the body where it rests.",
    "Let the out-breath be a little longer than the in-breath.",
    "Sounds arrive and pass. No need to follow them.",
    "Soften the jaw, the shoulders, the hands.",
    "Nothing to add. Just this breath.",
]
GUIDANCE_END = "That is the sit. One more breath before you move."

SCALES = {
    "mood": "how you feel",
    "energy": "how much you have in the tank",
    "stress": "how wound up you are",
}
SPARKS = "▁▂▃▄▅▆▇█"


class MindfulError(ValueError):
    """Raised for bad breathing patterns or check-in values."""


# ------------------------------------------------------------------ patterns
def parse_pattern(text: str):
    """``box`` or ``4-7-8`` or ``4-4-4-4`` -> ``(in, hold, out, hold_out, name)``."""
    raw = (text or "box").strip().lower()
    if raw in PATTERNS:
        inhale, hold, exhale, hold_out, _ = PATTERNS[raw]
        return inhale, hold, exhale, hold_out, raw
    normalised = raw.replace(":", "-").replace(",", "-")
    if not re.fullmatch(r"\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?){1,3}", normalised):
        raise MindfulError(
            "unknown pattern %r; use a name (%s) or counts like 4-7-8"
            % (text, ", ".join(sorted(PATTERNS))))
    counts = [float(part) for part in normalised.split("-")]
    if sum(counts) <= 0:
        raise MindfulError("a breathing pattern needs positive counts")
    if len(counts) == 2:
        # Two counts are the in-breath and the out-breath, with no pauses.
        counts = [counts[0], 0.0, counts[1], 0.0]
    elif len(counts) == 3:
        counts = counts + [0.0]
    return counts[0], counts[1], counts[2], counts[3], normalised


def pattern_phases(pattern):
    """The non-zero phases of one full breath, as ``(key, label, seconds)``."""
    inhale, hold, exhale, hold_out = pattern[:4]
    lengths = {"inhale": inhale, "hold": hold, "exhale": exhale, "hold_out": hold_out}
    return [(key, label, float(lengths[key])) for key, label in PHASE_LABELS if lengths[key] > 0]


def cycle_length(pattern) -> timedelta:
    return timedelta(seconds=sum(pattern[:4]))


def rounds_for(pattern, duration: timedelta) -> int:
    """How many whole breaths fit in a duration (at least one)."""
    seconds = cycle_length(pattern).total_seconds()
    if seconds <= 0:
        return 1
    return max(1, int(round(duration.total_seconds() / seconds)))


def describe_pattern(pattern) -> str:
    inhale, hold, exhale, hold_out, name = pattern
    counts = "-".join(("%g" % value) for value in (inhale, hold, exhale, hold_out))
    known = PATTERNS.get(name)
    if not known:
        return counts
    return "%s (%s) - %s" % (name, counts, known[4])


def breath_plan(pattern, rounds: int):
    """The whole session as ``(round, key, label, seconds)`` tuples."""
    phases = pattern_phases(pattern)
    plan = []
    for index in range(1, rounds + 1):
        for key, label, seconds in phases:
            plan.append((index, key, label, seconds))
    return plan


def _phase_fill(key: str, progress: float) -> float:
    """How full the breath bar should be, 0 to 1, part way through a phase."""
    if key == "inhale":
        return progress
    if key == "exhale":
        return 1.0 - progress
    return 1.0 if key == "hold" else 0.0


def breathe(pattern, rounds: int, term, quiet: bool = False, bell: bool = True,
            notify_on_end: bool = True, stream=None, width: int = 28,
            sleep=time.sleep, clock=time.monotonic) -> bool:
    """Run the pacer.  Returns False if the user stops it early."""
    import sys as _sys

    stream = stream or _sys.stdout
    interactive = bool(getattr(stream, "isatty", lambda: False)()) and not quiet
    plan = breath_plan(pattern, rounds)
    drawn = 0
    try:
        for index, key, label, seconds in plan:
            started = clock()
            while True:
                elapsed = clock() - started
                progress = min(1.0, elapsed / seconds) if seconds > 0 else 1.0
                if interactive:
                    filled = _phase_fill(key, progress)
                    lines = [
                        "%s   %s" % (term.paint("Round %d/%d" % (index, rounds), "bold"),
                                     term.paint(label, "cyan", "bold")),
                        "%s  %s" % (term.paint(bar(filled, 1.0, width, filled="●", empty="·"), "cyan"),
                                    term.paint("%0.0fs" % max(0, seconds - elapsed), "yellow")),
                    ]
                    if drawn:
                        stream.write("\033[%dA" % drawn)
                    for line in lines:
                        stream.write("\033[2K" + line + "\n")
                    drawn = len(lines)
                    stream.flush()
                if elapsed >= seconds:
                    break
                sleep(min(0.1, seconds - elapsed))
    except KeyboardInterrupt:
        if interactive:
            stream.write("\n")
        return False
    if not interactive and not quiet:
        stream.write("Finished %d rounds.\n" % rounds)
    notify("klok", "Breathing finished: %d rounds" % rounds,
           enabled=notify_on_end, bell=bell)
    return True


# ------------------------------------------------------------------- sitting
def sit_plan(duration: timedelta, interval: Optional[timedelta], warmup: timedelta = None):
    """Bell times, in seconds from the start, including the closing bell."""
    total = duration.total_seconds()
    marks = []
    if warmup and warmup.total_seconds() > 0:
        marks.append(warmup.total_seconds())
    if interval and interval.total_seconds() > 0:
        step = interval.total_seconds()
        mark = step
        while mark < total - 1:
            marks.append(mark)
            mark += step
    marks.append(total)
    return sorted(set(marks))


def meditate(duration: timedelta, term, interval: timedelta = None, warmup: timedelta = None,
             guidance: bool = True, quiet: bool = False, bell: bool = True,
             notify_on_end: bool = True, stream=None, sleep=time.sleep,
             clock=time.monotonic) -> bool:
    """A silent sit with a bell at the end and optionally along the way."""
    import sys as _sys

    stream = stream or _sys.stdout
    interactive = bool(getattr(stream, "isatty", lambda: False)()) and not quiet
    total = duration.total_seconds()
    marks = sit_plan(duration, interval, warmup)
    closing = marks[-1]

    if guidance and not quiet:
        for line in GUIDANCE_START:
            stream.write(term.paint(line.format(length=format_duration(duration)), "dim") + "\n")
        stream.write("\n")

    prompt_index = 0
    struck = set()
    drawn = 0
    try:
        started = clock()
        while True:
            elapsed = clock() - started
            for mark in marks:
                if mark not in struck and elapsed >= mark:
                    struck.add(mark)
                    if mark < closing:
                        notify("klok", "Interval bell", enabled=False, bell=bell)
                        if guidance and not quiet:
                            if drawn:
                                stream.write("\033[%dA" % drawn)
                                drawn = 0
                            line = GUIDANCE_INTERVAL[prompt_index % len(GUIDANCE_INTERVAL)]
                            prompt_index += 1
                            stream.write("\033[2K" + term.paint("  " + line, "dim") + "\n")
                            stream.flush()
            if interactive:
                remaining = max(0, total - elapsed)
                lines = [
                    "%s   %s" % (term.paint("Sitting", "cyan", "bold"),
                                 term.paint(format_duration(timedelta(seconds=int(remaining))), "yellow")),
                    term.paint(bar(elapsed, total, 30, filled="─", empty=" "), "cyan"),
                ]
                if drawn:
                    stream.write("\033[%dA" % drawn)
                for line in lines:
                    stream.write("\033[2K" + line + "\n")
                drawn = len(lines)
                stream.flush()
            if elapsed >= total:
                break
            sleep(min(0.2, total - elapsed))
    except KeyboardInterrupt:
        if interactive:
            stream.write("\n")
        return False

    notify("klok", "Sit finished (%s)" % format_duration(duration),
           enabled=notify_on_end, bell=bell)
    if guidance and not quiet:
        stream.write("\n" + term.paint(GUIDANCE_END, "dim") + "\n")
    return True


# ---------------------------------------------------------------- check-ins
@dataclass
class Checkin:
    """A point-in-time note on how you are doing."""

    at: datetime
    mood: Optional[int] = None
    energy: Optional[int] = None
    stress: Optional[int] = None
    note: str = ""
    tags: List[str] = field(default_factory=list)
    id: str = ""

    def __post_init__(self):
        if not self.id:
            import uuid
            self.id = uuid.uuid4().hex[:8]

    def to_dict(self) -> dict:
        return {"id": self.id, "at": to_iso(self.at), "mood": self.mood,
                "energy": self.energy, "stress": self.stress,
                "note": self.note, "tags": list(self.tags)}

    @classmethod
    def from_dict(cls, data: dict) -> "Checkin":
        return cls(at=from_iso(data["at"]), mood=_as_score(data.get("mood")),
                   energy=_as_score(data.get("energy")), stress=_as_score(data.get("stress")),
                   note=data.get("note") or "", tags=list(data.get("tags") or []),
                   id=str(data.get("id") or ""))


def _as_score(value):
    if value in (None, ""):
        return None
    return int(value)


def check_score(name: str, value):
    """Validate one of the 1-5 scales."""
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise MindfulError("%s must be a whole number from 1 to 5" % name)
    if not 1 <= number <= 5:
        raise MindfulError("%s must be between 1 and 5, got %s" % (name, value))
    return number


class CheckinStore:
    """Append-mostly JSONL store for check-ins, alongside the frames file."""

    def __init__(self, config):
        self.config = config
        self.path = Path(config.home) / "checkins.jsonl"
        self._items = None

    @property
    def items(self) -> List[Checkin]:
        if self._items is None:
            self._items = self._load()
        return self._items

    def _load(self) -> List[Checkin]:
        if not self.path.exists():
            return []
        found = []
        with self.path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    found.append(Checkin.from_dict(json.loads(line)))
                except (ValueError, KeyError) as exc:
                    raise MindfulError("%s line %d is not a valid check-in: %s"
                                       % (self.path, number, exc))
        found.sort(key=lambda item: item.at)
        return found

    def add(self, checkin: Checkin) -> Checkin:
        self.items.append(checkin)
        self._items.sort(key=lambda item: item.at)
        return checkin

    def remove(self, reference: str) -> Checkin:
        matches = [item for item in self.items if item.id == reference
                   or item.id.startswith(reference.lower())]
        if reference == "@" and self.items:
            matches = [self.items[-1]]
        if not matches:
            raise MindfulError("no check-in matching %r" % reference)
        if len(matches) > 1:
            raise MindfulError("%r is ambiguous (%s)" % (reference, ", ".join(m.id for m in matches)))
        self._items = [item for item in self.items if item.id != matches[0].id]
        return matches[0]

    def save(self) -> None:
        self.config.ensure_home()
        tmp = self.path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for item in sorted(self.items, key=lambda entry: entry.at):
                handle.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)

    def select(self, start: datetime = None, end: datetime = None) -> List[Checkin]:
        return [item for item in self.items
                if (start is None or item.at >= start) and (end is None or item.at < end)]


# ----------------------------------------------------------------- reporting
def sparkline(values) -> str:
    """A one-line trend for a list of numbers, gaps allowed as ``None``."""
    present = [value for value in values if value is not None]
    if not present:
        return ""
    low, high = min(present), max(present)
    span = (high - low) or 1
    out = []
    for value in values:
        if value is None:
            out.append(" ")
        else:
            index = int(round((value - low) / span * (len(SPARKS) - 1)))
            out.append(SPARKS[index])
    return "".join(out)


def daily_averages(checkins, field_name: str):
    """``{date: average}`` for one scale, skipping days with no answer."""
    buckets = {}
    for item in checkins:
        value = getattr(item, field_name)
        if value is None:
            continue
        buckets.setdefault(item.at.date(), []).append(value)
    return {day: sum(values) / len(values) for day, values in sorted(buckets.items())}


def streak(days, today) -> int:
    """Consecutive days with practice, counting back from today or yesterday."""
    known = set(days)
    if not known:
        return 0
    cursor = today
    if cursor not in known:
        cursor = today - timedelta(days=1)
        if cursor not in known:
            return 0
    count = 0
    while cursor in known:
        count += 1
        cursor -= timedelta(days=1)
    return count


def longest_streak(days) -> int:
    ordered = sorted(set(days))
    best = run = 0
    previous = None
    for day in ordered:
        run = run + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        best = max(best, run)
        previous = day
    return best


def break_due(frame, after: timedelta, now: datetime) -> bool:
    """True when a single stretch of work has run past the break threshold."""
    if frame is None or after.total_seconds() <= 0:
        return False
    return frame.duration(now) >= after
