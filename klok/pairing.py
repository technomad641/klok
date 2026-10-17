"""Pairing helpers: match a check-in to the frame that was running at a
given moment, and classify practice (breathing/meditation) sessions.

These were born in ``dashboard.py``; they live here so the dashboard, the
``review`` ritual and the ``insights`` digest all share one join and agree
on what "the block this check-in belongs to" means.  The join is
timestamp-based and half-open (``start <= moment < end``), and ties between
overlapping frames go to the latest-started one.
"""

from datetime import datetime
from typing import List, Optional

from klok.mindful import BREATHING_TAG, MEDITATION_TAG
from klok.model import Frame

PRACTICE_TAGS = {BREATHING_TAG, MEDITATION_TAG}


def is_practice(frame: Frame, practice_sheet: str = "") -> bool:
    """Breathing and sitting sessions, however they were recorded."""
    return bool(PRACTICE_TAGS.intersection(frame.tags)) or (
        bool(practice_sheet) and frame.sheet == practice_sheet)


def frame_at(frames: List[Frame], moment: datetime, now: datetime) -> Optional[Frame]:
    """The frame that was running at ``moment``; the latest-started one wins.

    Half-open: a frame covers ``start <= moment < end``.  ``now`` resolves the
    end of a still-running frame.  When frames overlap, the one that started
    most recently takes the moment, which is why callers that want an
    unambiguous reverse lookup timestamp a check-in at the block's *start*.
    """
    found = None
    for frame in frames:
        if frame.start <= moment < frame.end_or(now):
            if found is None or frame.start >= found.start:
                found = frame
    return found
