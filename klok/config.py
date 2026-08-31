"""Configuration and on-disk locations.

Everything klok keeps lives under a single data directory so a whole
install can be backed up, synced or version-controlled by copying one
folder.  ``KLOK_HOME`` overrides it, which is also what the test-suite
uses to stay away from real data.
"""

import configparser
import os
from pathlib import Path

DEFAULTS = {
    "general": {
        # Project used by `klok start` when none is given.
        "default_project": "",
        # monday or sunday - drives :week and the week charts.
        "week_start": "monday",
        # Round durations to a multiple of this many minutes in reports (0 = off).
        "round": "0",
        # 24 or 12 hour clock in rendered output.
        "time_format": "24",
        # Stop a running frame automatically when a new one is started.
        "autostop": "true",
        # auto, always or never.
        "color": "auto",
        # Overrides $VISUAL / $EDITOR for `klok edit`.
        "editor": "",
        # Refuse to create a project that has never been seen before.
        "confirm_new_project": "false",
    },
    "exclusions": {
        # Hours considered "working hours"; `klok gaps` ignores anything outside.
        "hours": "",
        # Days that are never expected to hold tracked time.
        "days": "",
        # Comma separated YYYY-MM-DD dates treated like excluded days.
        "holidays": "",
    },
    "focus": {
        "work": "25m",
        "break": "5m",
        "long_break": "15m",
        "rounds": "4",
        "notify": "true",
        "bell": "true",
        # Record focus sessions as tracked frames.
        "track": "true",
    },
}

TRUTHY = {"1", "true", "yes", "on", "y"}


def data_home() -> Path:
    """Directory holding frames, state, config and the undo stack."""
    env = os.environ.get("KLOK_HOME")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "klok"


class Config:
    def __init__(self, home: Path = None):
        self.home = Path(home) if home else data_home()
        self.path = self.home / "config.ini"
        self._parser = configparser.ConfigParser()
        self._parser.read_dict(DEFAULTS)
        if self.path.exists():
            self._parser.read(self.path, encoding="utf-8")

    # -- paths ---------------------------------------------------------
    @property
    def frames_path(self) -> Path:
        return self.home / "frames.jsonl"

    @property
    def state_path(self) -> Path:
        return self.home / "state.json"

    @property
    def undo_path(self) -> Path:
        return self.home / "undo.jsonl"

    def ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)

    # -- values --------------------------------------------------------
    def get(self, key: str, fallback=None):
        section, _, option = key.partition(".")
        if not option:
            raise KeyError("config keys look like 'section.option', got %r" % key)
        try:
            return self._parser.get(section, option)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return fallback

    def get_bool(self, key: str, fallback: bool = False) -> bool:
        raw = self.get(key)
        if raw is None or raw == "":
            return fallback
        return str(raw).strip().lower() in TRUTHY

    def get_int(self, key: str, fallback: int = 0) -> int:
        raw = self.get(key)
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return fallback

    def get_list(self, key: str):
        raw = self.get(key) or ""
        return [part.strip() for part in raw.split(",") if part.strip()]

    def set(self, key: str, value: str) -> None:
        section, _, option = key.partition(".")
        if not option:
            raise KeyError("config keys look like 'section.option', got %r" % key)
        if not self._parser.has_section(section):
            self._parser.add_section(section)
        self._parser.set(section, option, value)

    def unset(self, key: str) -> bool:
        section, _, option = key.partition(".")
        try:
            return self._parser.remove_option(section, option)
        except configparser.NoSectionError:
            return False

    def items(self):
        for section in sorted(self._parser.sections()):
            for option, value in sorted(self._parser.items(section)):
                yield "%s.%s" % (section, option), value

    def save(self) -> None:
        self.ensure_home()
        tmp = self.path.with_suffix(".ini.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            self._parser.write(handle)
        os.replace(tmp, self.path)
