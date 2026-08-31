"""Countdown timers, pomodoro rounds and desktop notifications.

This is the part that covers the "minimal timer" tools - a visible
countdown, a big clock, a nudge when the time is up - and it feeds the
same frame store as everything else so focus sessions show up in reports.
"""

import shutil
import subprocess
import sys
import time
from datetime import timedelta

from klok.timeparse import format_duration
from klok.utils import bar

BIG_DIGITS = {
    "0": ["█▀▀█", "█  █", "█  █", "█  █", "█▄▄█"],
    "1": ["  ▀█", "   █", "   █", "   █", "   █"],
    "2": ["█▀▀█", "   █", " ▄▀ ", "▄▀  ", "█▄▄▄"],
    "3": ["█▀▀█", "   █", " ▀▀▄", "   █", "█▄▄█"],
    "4": ["█  █", "█  █", "█▄▄█", "   █", "   █"],
    "5": ["█▀▀▀", "█▄▄ ", "   █", "   █", "▀▄▄▀"],
    "6": ["█▀▀▀", "█   ", "█▀▀█", "█  █", "█▄▄█"],
    "7": ["▀▀▀█", "   █", "  ▄▀", " ▄▀ ", "▄▀  "],
    "8": ["█▀▀█", "█  █", "█▄▄█", "█  █", "█▄▄█"],
    "9": ["█▀▀█", "█  █", "▀▀▀█", "   █", "▄▄▄█"],
    ":": ["    ", " ██ ", "    ", " ██ ", "    "],
    " ": ["    ", "    ", "    ", "    ", "    "],
}

ART = {
    "coffee": [
        "        (  )   (   )  )",
        "         ) (   )  (  (",
        "         ( )  (    ) )",
        "         _____________",
        "        <_____________> ___",
        "        |             |/ _ \\",
        "        |               | | |",
        "        |               |_| |",
        "     ___|             |\\___/",
        "    /    \\___________/    \\",
        "    \\_____________________/",
    ],
    "tree": [
        "         &&& &&  & &&",
        "     && &\\/&\\|& ()|/ @, &&",
        "     &\\/(/&/&||/& /_/)_&/_&",
        "  &() &\\/&|()|/&\\/ '%\" & ()",
        " &_\\_&&_\\ |& |&&/&__%_/_& &&",
        "&&   && & &| &| /& & % ()& /&&",
        "          |||   |||",
    ],
    "cat": [
        "     /\\_/\\",
        "    ( o.o )",
        "     > ^ <",
    ],
    "none": [],
}


def notify(title: str, message: str, enabled: bool = True, bell: bool = True) -> None:
    """Best-effort desktop notification; never fails the command."""
    if bell:
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass
    if not enabled:
        return
    commands = []
    if shutil.which("notify-send"):
        commands.append(["notify-send", title, message])
    if shutil.which("terminal-notifier"):
        commands.append(["terminal-notifier", "-title", title, "-message", message])
    if sys.platform == "darwin" and shutil.which("osascript"):
        script = 'display notification "%s" with title "%s"' % (
            message.replace('"', "'"), title.replace('"', "'"))
        commands.append(["osascript", "-e", script])
    if sys.platform.startswith("win") and shutil.which("powershell"):
        commands.append(["powershell", "-NoProfile", "-Command",
                         "[console]::beep(880,300); Write-Host '%s: %s'" % (title, message)])
    for command in commands:
        try:
            subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            continue


def big_clock(text: str) -> str:
    """Render ``MM:SS`` in block characters, arttime style."""
    rows = ["" for _ in range(5)]
    for char in text:
        glyph = BIG_DIGITS.get(char, BIG_DIGITS[" "])
        for index in range(5):
            rows[index] += glyph[index] + " "
    return "\n".join(row.rstrip() for row in rows)


def _clock_text(remaining: timedelta) -> str:
    total = max(0, int(remaining.total_seconds()))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, seconds)
    return "%02d:%02d" % (minutes, seconds)


def countdown(duration: timedelta, label: str, term, art: str = None,
              big: bool = False, quiet: bool = False, notify_on_end: bool = True,
              bell: bool = True, stream=None) -> bool:
    """Run a countdown.  Returns False when the user interrupts it."""
    stream = stream or sys.stdout
    total_seconds = max(1, int(duration.total_seconds()))
    started = time.monotonic()
    art_lines = ART.get(art or "", []) if art else []
    interactive = bool(getattr(stream, "isatty", lambda: False)()) and not quiet
    drawn_lines = 0

    try:
        while True:
            elapsed = time.monotonic() - started
            remaining = timedelta(seconds=max(0, total_seconds - int(elapsed)))
            if interactive:
                block = []
                if art_lines:
                    block.extend(art_lines)
                    block.append("")
                if big:
                    block.extend(big_clock(_clock_text(remaining)).splitlines())
                    block.append("")
                width = min(40, max(20, term.width() - 30))
                block.append("%s  %s  %s" % (
                    term.paint(label, "bold"),
                    term.paint(bar(total_seconds - remaining.total_seconds(), total_seconds, width), "cyan"),
                    term.paint(_clock_text(remaining), "yellow")))
                if drawn_lines:
                    stream.write("\033[%dA" % drawn_lines)
                for line in block:
                    stream.write("\033[2K" + line + "\n")
                drawn_lines = len(block)
                stream.flush()
            if remaining.total_seconds() <= 0:
                break
            time.sleep(min(1.0, max(0.05, total_seconds - elapsed)))
    except KeyboardInterrupt:
        if interactive:
            stream.write("\n")
        return False

    if not interactive and not quiet:
        stream.write("%s finished (%s)\n" % (label, format_duration(duration)))
    notify("klok", "%s finished (%s)" % (label, format_duration(duration)),
           enabled=notify_on_end, bell=bell)
    return True


def pomodoro_plan(rounds: int, work: timedelta, short_break: timedelta, long_break: timedelta):
    """Yield ``(kind, label, duration)`` for a full pomodoro cycle."""
    for index in range(1, rounds + 1):
        yield ("work", "Focus %d/%d" % (index, rounds), work)
        if index == rounds:
            yield ("long_break", "Long break", long_break)
        else:
            yield ("break", "Break %d" % index, short_break)
