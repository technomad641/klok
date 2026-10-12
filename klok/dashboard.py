"""Hours & Feelings: an offline HTML dashboard of tracked time and how it felt.

The page itself is a single static file (``dashboard.html``) shipped with the
package.  This module turns frames and check-ins into the JSON it expects and
splices that in, so the result opens straight from disk with no server and
nothing fetched.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from klok import __version__
from klok.mindful import BREATHING_TAG, MEDITATION_TAG, Checkin
from klok.model import Frame

TEMPLATE = "dashboard.html"
DATA_MARKER = "/*KLOK_DATA*/null"
PRACTICE_TAGS = {BREATHING_TAG, MEDITATION_TAG}
NO_PROJECT = "(no project)"


def template() -> str:
    return (Path(__file__).parent / TEMPLATE).read_text(encoding="utf-8")


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def is_practice(frame: Frame, practice_sheet: str = "") -> bool:
    """Breathing and sitting sessions, however they were recorded."""
    return bool(PRACTICE_TAGS.intersection(frame.tags)) or (
        bool(practice_sheet) and frame.sheet == practice_sheet)


def frame_at(frames: List[Frame], moment: datetime, now: datetime) -> Optional[Frame]:
    """The frame that was running at ``moment``; the latest-started one wins."""
    found = None
    for frame in frames:
        if frame.start <= moment < frame.end_or(now):
            if found is None or frame.start >= found.start:
                found = frame
    return found


def _mean(values) -> Optional[float]:
    present = [value for value in values if value is not None]
    return round(sum(present) / len(present), 2) if present else None


def build_payload(frames: List[Frame], checkins: List[Checkin], now: datetime,
                  deep_tags=("deep", "focus"), deep_min: timedelta = timedelta(minutes=25),
                  deep_goal: timedelta = timedelta(minutes=90),
                  practice_sheet: str = "") -> dict:
    """Everything the page needs, with each check-in joined to its frame.

    Deep work is any session of at least ``deep_min`` carrying one of
    ``deep_tags``.  If nothing in the data carries those tags, every
    non-practice session that long counts instead, so the streak still means
    something before you start tagging.
    """
    frames = sorted(frames, key=lambda frame: frame.start)
    name = lambda frame: frame.project or NO_PROJECT  # noqa: E731

    tagged = [tag.lstrip("+") for tag in deep_tags]
    uses_tags = any(set(tagged).intersection(frame.tags) for frame in frames)
    if uses_tags:
        basis = "work tagged " + " or ".join("+" + tag for tag in tagged)
    else:
        basis = "any tracked work (tag it %s to narrow this)" % (
            "+" + tagged[0] if tagged else "+deep")

    def deep(frame: Frame) -> bool:
        if frame.duration(now) < deep_min or is_practice(frame, practice_sheet):
            return False
        return bool(set(tagged).intersection(frame.tags)) if uses_tags else True

    joined = [(checkin, frame_at(frames, checkin.at, now)) for checkin in checkins]

    seconds, practice_seconds, energies = {}, {}, {}
    for frame in frames:
        key, length = name(frame), frame.duration(now).total_seconds()
        seconds[key] = seconds.get(key, 0) + length
        if is_practice(frame, practice_sheet):
            practice_seconds[key] = practice_seconds.get(key, 0) + length
    for checkin, frame in joined:
        if frame is not None:
            energies.setdefault(name(frame), []).append(checkin.energy)

    projects = [{"name": key,
                 "practice": practice_seconds.get(key, 0) * 2 > seconds[key],
                 "energy": _mean(energies.get(key, []))}
                for key in sorted(seconds, key=lambda key: (-seconds[key], key))]

    return {
        "version": __version__,
        "now": _iso(now),
        "projects": projects,
        "frames": [{"start": _iso(frame.start), "stop": _iso(frame.end_or(now)),
                    "project": name(frame), "sheet": frame.sheet,
                    "tags": list(frame.tags), "deep": deep(frame)}
                   for frame in frames],
        "checkins": [{"at": _iso(checkin.at), "mood": checkin.mood,
                      "energy": checkin.energy, "stress": checkin.stress,
                      "project": name(frame) if frame is not None else None}
                     for checkin, frame in joined],
        "deep": {"min": int(deep_min.total_seconds()), "goal": int(deep_goal.total_seconds()),
                 "basis": basis},
    }


def render_page(payload: Optional[dict]) -> str:
    """The dashboard HTML; ``None`` leaves the page on its built-in demo data."""
    page = template()
    if payload is None:
        return page
    # "</" inside a <script> could close it early; JSON allows "<\/" instead.
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return page.replace(DATA_MARKER, data, 1)
