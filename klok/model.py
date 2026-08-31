"""The single record type klok stores: a Frame."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import List, Optional

from klok.timeparse import from_iso, to_iso

DEFAULT_SHEET = "default"


def new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Frame:
    """One tracked interval.  ``stop`` is ``None`` while it is running."""

    start: datetime
    stop: Optional[datetime] = None
    project: str = ""
    tags: List[str] = field(default_factory=list)
    note: str = ""
    sheet: str = DEFAULT_SHEET
    id: str = field(default_factory=new_id)
    updated_at: Optional[datetime] = None

    # -- derived -------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.stop is None

    def end_or(self, now: datetime) -> datetime:
        """Stop time, or ``now`` for a frame that is still running."""
        return self.stop if self.stop is not None else now

    def duration(self, now: Optional[datetime] = None) -> timedelta:
        end = self.stop
        if end is None:
            if now is None:
                return timedelta(0)
            end = now
        span = end - self.start
        return span if span.total_seconds() > 0 else timedelta(0)

    def overlaps(self, other: "Frame", now: datetime) -> bool:
        a_end = self.end_or(now)
        b_end = other.end_or(now)
        return self.start < b_end and other.start < a_end

    def spans(self, start: datetime, end: datetime, now: datetime) -> bool:
        """True when any part of the frame falls inside [start, end)."""
        return self.start < end and start < self.end_or(now)

    def label(self) -> str:
        parts = [self.project or "(no project)"]
        if self.tags:
            parts.append(" ".join("+" + tag for tag in self.tags))
        return " ".join(parts)

    def touch(self, when: datetime) -> None:
        self.updated_at = when

    def copy(self, **changes) -> "Frame":
        return replace(self, **changes)

    # -- serialisation -------------------------------------------------
    def to_dict(self) -> dict:
        data = {
            "id": self.id,
            "start": to_iso(self.start),
            "stop": to_iso(self.stop) if self.stop else None,
            "project": self.project,
            "tags": list(self.tags),
            "note": self.note,
            "sheet": self.sheet,
        }
        if self.updated_at:
            data["updated_at"] = to_iso(self.updated_at)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Frame":
        stop = data.get("stop") or data.get("end")
        updated = data.get("updated_at")
        tags = data.get("tags") or []
        if isinstance(tags, str):
            tags = [t for t in tags.replace(",", " ").split() if t]
        return cls(
            start=from_iso(data["start"]),
            stop=from_iso(stop) if stop else None,
            project=(data.get("project") or "").strip(),
            tags=[str(t).lstrip("+") for t in tags],
            note=data.get("note") or data.get("annotation") or "",
            sheet=(data.get("sheet") or DEFAULT_SHEET).strip() or DEFAULT_SHEET,
            id=str(data.get("id") or new_id()),
            updated_at=from_iso(updated) if updated else None,
        )


def normalise_tags(tags) -> List[str]:
    """De-duplicate and strip a ``+``/``-`` prefix, preserving order."""
    seen, out = set(), []
    for tag in tags or []:
        clean = str(tag).strip().lstrip("+")
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out
