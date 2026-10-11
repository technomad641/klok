"""The plaintext store: one JSON object per line, newest last.

The file is meant to survive contact with humans - it is readable, it
diffs well in git, and a botched hand-edit only ever costs the lines that
were touched.  Every mutation snapshots the previous state so ``klok
undo`` can walk backwards.
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional

from klok.model import DEFAULT_SHEET, Frame
from klok.timeparse import now_local

UNDO_LIMIT = 100


class StoreError(Exception):
    """Raised for user-visible storage problems (bad ids, locks, corruption)."""


class FileLock:
    """A tiny cross-platform advisory lock built on exclusive file creation."""

    def __init__(self, path: Path, timeout: float = 5.0):
        self.path = Path(str(path) + ".lock")
        self.timeout = timeout
        self._fd = None

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                if self._stale():
                    self.path.unlink(missing_ok=True)
                    continue
                if time.time() >= deadline:
                    raise StoreError("another klok process is holding %s" % self.path)
                time.sleep(0.05)

    def _stale(self) -> bool:
        try:
            return time.time() - self.path.stat().st_mtime > 30
        except OSError:
            return True

    def __exit__(self, *exc):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.path.unlink(missing_ok=True)
        return False


class Store:
    def __init__(self, config):
        self.config = config
        self.path = config.frames_path
        self._frames: Optional[List[Frame]] = None
        # The raw, non-blank lines currently on disk, kept so a mutation can
        # snapshot the previous state for undo without re-reading the file.
        self._disk_lines: Optional[List[str]] = None
        self._pending_label = None

    # -- loading / saving ----------------------------------------------
    @property
    def frames(self) -> List[Frame]:
        if self._frames is None:
            self._frames = self._load()
        return self._frames

    def _load(self) -> List[Frame]:
        if not self.path.exists():
            self._disk_lines = []
            return []
        frames = []
        raw = []
        with self.path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                raw.append(line.rstrip("\n"))
                if stripped.startswith("#"):
                    continue
                try:
                    frames.append(Frame.from_dict(json.loads(stripped)))
                except (ValueError, KeyError) as exc:
                    raise StoreError("%s line %d is not a valid frame: %s" % (self.path, number, exc))
        self._disk_lines = raw
        frames.sort(key=lambda frame: frame.start)
        return frames

    def reload(self) -> None:
        self._frames = None
        self._disk_lines = None

    def save(self, label: str = "") -> None:
        """Write frames back, snapshotting the previous file for undo first."""
        self.config.ensure_home()
        with FileLock(self.path):
            self._snapshot(label or self._pending_label or "change")
            tmp = self.path.with_suffix(".jsonl.tmp")
            written = []
            with tmp.open("w", encoding="utf-8") as handle:
                for frame in sorted(self.frames, key=lambda item: item.start):
                    line = json.dumps(frame.to_dict(), ensure_ascii=False)
                    handle.write(line + "\n")
                    written.append(line)
            os.replace(tmp, self.path)
        # Disk now holds exactly these lines; remember them so a second save in
        # the same process can snapshot without another read.
        self._disk_lines = written
        self._pending_label = None

    # -- undo -----------------------------------------------------------
    # Each mutation drops one snapshot file into ``undo.d/``; the newest has
    # the highest sequence number.  This keeps a mutation's undo cost at one
    # small write plus a directory listing, instead of rewriting a file that
    # held every snapshot.
    def _snapshot(self, label: str) -> None:
        if self._disk_lines is not None:
            previous = list(self._disk_lines)
        elif self.path.exists():
            with self.path.open("r", encoding="utf-8") as handle:
                previous = [line.rstrip("\n") for line in handle if line.strip()]
        else:
            previous = []
        self._migrate_legacy_undo()
        entry = {"at": now_local().isoformat(), "label": label, "lines": previous}
        undo_dir = self.config.undo_dir
        undo_dir.mkdir(parents=True, exist_ok=True)
        existing = self._snapshot_files()
        seq = existing[-1][0] + 1 if existing else 1
        path = undo_dir / ("%012d.json" % seq)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        existing.append((seq, path))
        for _, stale in existing[:-UNDO_LIMIT]:
            try:
                stale.unlink()
            except OSError:
                pass

    def _snapshot_files(self):
        """Snapshot files as ``(sequence, path)`` pairs, oldest first."""
        undo_dir = self.config.undo_dir
        if not undo_dir.exists():
            return []
        items = []
        for child in undo_dir.iterdir():
            if child.suffix == ".json" and child.stem.isdigit():
                items.append((int(child.stem), child))
        items.sort()
        return items

    def _migrate_legacy_undo(self) -> None:
        """One-time move of a pre-1.1 single-file undo stack into ``undo.d/``."""
        legacy = self.config.home / "undo.jsonl"
        if not legacy.exists() or self._snapshot_files():
            return
        entries = []
        try:
            with legacy.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return
        undo_dir = self.config.undo_dir
        undo_dir.mkdir(parents=True, exist_ok=True)
        for seq, entry in enumerate(entries[-UNDO_LIMIT:], 1):
            (undo_dir / ("%012d.json" % seq)).write_text(
                json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
        legacy.unlink(missing_ok=True)

    def undo(self) -> Optional[str]:
        """Restore the previous state.  Returns the label that was undone."""
        self._migrate_legacy_undo()
        files = self._snapshot_files()
        if not files:
            return None
        _, path = files[-1]
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            return None
        self.config.ensure_home()
        with FileLock(self.path):
            tmp = self.path.with_suffix(".jsonl.tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                for line in entry.get("lines", []):
                    handle.write(line + "\n")
            os.replace(tmp, self.path)
            path.unlink(missing_ok=True)
        self.reload()
        return entry.get("label", "change")

    # -- state (active sheet) -------------------------------------------
    def _state(self) -> dict:
        path = self.config.state_path
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def _write_state(self, state: dict) -> None:
        self.config.ensure_home()
        tmp = self.config.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.config.state_path)

    @property
    def current_sheet(self) -> str:
        return self._state().get("sheet") or DEFAULT_SHEET

    @property
    def last_sheet(self) -> str:
        return self._state().get("last_sheet") or DEFAULT_SHEET

    def set_sheet(self, name: str) -> str:
        state = self._state()
        previous = state.get("sheet") or DEFAULT_SHEET
        if name == "-":
            name = state.get("last_sheet") or DEFAULT_SHEET
        state["last_sheet"] = previous
        state["sheet"] = name
        self._write_state(state)
        return name

    def sheets(self) -> List[str]:
        names = {frame.sheet for frame in self.frames}
        names.add(self.current_sheet)
        return sorted(names)

    # -- mutation --------------------------------------------------------
    def add(self, frame: Frame) -> Frame:
        frame.touch(now_local())
        self.frames.append(frame)
        self._frames.sort(key=lambda item: item.start)
        return frame

    def remove(self, frame: Frame) -> None:
        self._frames = [item for item in self.frames if item.id != frame.id]

    def replace(self, frames: Iterable[Frame]) -> None:
        self._frames = sorted(frames, key=lambda item: item.start)

    # -- queries ---------------------------------------------------------
    def running(self, sheet: str = None) -> Optional[Frame]:
        candidates = [frame for frame in self.frames if frame.running]
        if sheet:
            candidates = [frame for frame in candidates if frame.sheet == sheet]
        return candidates[-1] if candidates else None

    def any_running(self) -> Optional[Frame]:
        return self.running(None)

    def last(self, sheet: str = None) -> Optional[Frame]:
        candidates = self.frames
        if sheet:
            candidates = [frame for frame in candidates if frame.sheet == sheet]
        return candidates[-1] if candidates else None

    def by_id(self, ref: str, sheet: str = None) -> Frame:
        """Resolve ``@`` (running/last), ``@N`` (Nth newest) or an id prefix."""
        ref = str(ref).strip()
        pool = [frame for frame in self.frames if sheet is None or frame.sheet == sheet]
        if not pool:
            raise StoreError("no entries recorded yet")
        if ref in ("@", "last", "current"):
            return self.running(sheet) or pool[-1]
        if ref.startswith("@") and ref[1:].isdigit():
            index = int(ref[1:])
            if index < 1 or index > len(pool):
                raise StoreError("no entry @%d (only %d recorded)" % (index, len(pool)))
            return list(reversed(pool))[index - 1]
        matches = [frame for frame in pool if frame.id == ref]
        if not matches:
            matches = [frame for frame in pool if frame.id.startswith(ref.lower())]
        if not matches:
            raise StoreError("no entry matching %r" % ref)
        if len(matches) > 1:
            raise StoreError("%r is ambiguous (%s)" % (ref, ", ".join(f.id for f in matches)))
        return matches[0]

    def select(self, start: datetime = None, end: datetime = None, sheet: str = None,
               projects=None, tags=None, exclude_tags=None, contains: str = None,
               now: datetime = None) -> List[Frame]:
        now = now or now_local()
        results = []
        for frame in self.frames:
            if sheet is not None and frame.sheet != sheet:
                continue
            if start is not None and end is not None and not frame.spans(start, end, now):
                continue
            if projects and not any(_matches(frame.project, pattern) for pattern in projects):
                continue
            if tags and not all(tag.lstrip("+") in frame.tags for tag in tags):
                continue
            if exclude_tags and any(tag.lstrip("-") in frame.tags for tag in exclude_tags):
                continue
            if contains:
                haystack = " ".join([frame.project, frame.note, " ".join(frame.tags)]).lower()
                if contains.lower() not in haystack:
                    continue
            results.append(frame)
        return results

    def projects(self, sheet: str = None) -> List[str]:
        return sorted({frame.project for frame in self.frames
                       if frame.project and (sheet is None or frame.sheet == sheet)})

    def tags(self, sheet: str = None) -> List[str]:
        found = set()
        for frame in self.frames:
            if sheet is None or frame.sheet == sheet:
                found.update(frame.tags)
        return sorted(found)

    def overlaps(self, now: datetime = None) -> List[tuple]:
        """Pairs of frames on the same sheet whose intervals intersect."""
        now = now or now_local()
        found = []
        ordered = sorted(self.frames, key=lambda frame: frame.start)
        for index, frame in enumerate(ordered):
            for other in ordered[index + 1:]:
                if other.start >= frame.end_or(now):
                    break
                if frame.sheet == other.sheet and frame.overlaps(other, now):
                    found.append((frame, other))
        return found

    def gaps(self, start: datetime, end: datetime, sheet: str = None,
             now: datetime = None, minimum: timedelta = timedelta(minutes=1)) -> List[tuple]:
        """Untracked stretches inside a range, as ``(start, end)`` pairs."""
        now = now or now_local()
        frames = sorted(self.select(start, end, sheet=sheet, now=now), key=lambda f: f.start)
        cursor = start
        found = []
        for frame in frames:
            if frame.start > cursor and frame.start - cursor >= minimum:
                found.append((cursor, min(frame.start, end)))
            cursor = max(cursor, frame.end_or(now))
            if cursor >= end:
                break
        boundary = min(end, now)
        if cursor < boundary and boundary - cursor >= minimum:
            found.append((cursor, boundary))
        return found


def _matches(value: str, pattern: str) -> bool:
    """Project filters match exactly, or as a ``parent`` prefix, or by glob."""
    import fnmatch

    value = value or ""
    pattern = pattern or ""
    if value == pattern:
        return True
    if any(char in pattern for char in "*?["):
        return fnmatch.fnmatch(value, pattern)
    return value.startswith(pattern + ".") or value.startswith(pattern + "/")
