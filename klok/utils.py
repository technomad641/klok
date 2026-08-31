"""Terminal helpers: colour, tables and bars."""

import os
import sys

RESET = "\033[0m"
CODES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "grey": "\033[90m",
}
PALETTE = ["cyan", "green", "yellow", "magenta", "blue", "red", "white"]


class Term:
    def __init__(self, mode: str = "auto", stream=None):
        self.stream = stream or sys.stdout
        if mode == "always":
            self.enabled = True
        elif mode == "never" or os.environ.get("NO_COLOR"):
            self.enabled = False
        else:
            self.enabled = bool(getattr(self.stream, "isatty", lambda: False)())

    def paint(self, text: str, *styles) -> str:
        if not self.enabled or not styles:
            return text
        prefix = "".join(CODES.get(style, "") for style in styles)
        return "%s%s%s" % (prefix, text, RESET) if prefix else text

    def project_color(self, project: str) -> str:
        if not project:
            return "grey"
        return PALETTE[sum(ord(char) for char in project) % len(PALETTE)]

    def width(self, default: int = 80) -> int:
        try:
            return max(40, os.get_terminal_size().columns)
        except OSError:
            return default


def visible_length(text: str) -> int:
    """Length of ``text`` ignoring ANSI escapes."""
    length, index = 0, 0
    while index < len(text):
        if text[index] == "\033":
            while index < len(text) and text[index] != "m":
                index += 1
            index += 1
            continue
        length += 1
        index += 1
    return length


def pad(text: str, width: int, align: str = "left") -> str:
    filler = " " * max(0, width - visible_length(text))
    if align == "right":
        return filler + text
    if align == "center":
        half = len(filler) // 2
        return " " * half + text + " " * (len(filler) - half)
    return text + filler


def render_table(rows, headers=None, aligns=None, term: Term = None, indent: str = "") -> str:
    """Render an aligned, borderless table (the shape most CLIs settle on)."""
    rows = [[("" if cell is None else str(cell)) for cell in row] for row in rows]
    if not rows and not headers:
        return ""
    columns = max([len(row) for row in rows] + [len(headers or [])])
    for row in rows:
        row.extend([""] * (columns - len(row)))
    aligns = list(aligns or []) + ["left"] * (columns - len(aligns or []))
    widths = [0] * columns
    if headers:
        headers = list(headers) + [""] * (columns - len(headers))
        for index, header in enumerate(headers):
            widths[index] = visible_length(str(header))
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], visible_length(cell))

    lines = []
    if headers:
        cells = [pad(str(header), widths[index], aligns[index]) for index, header in enumerate(headers)]
        line = indent + "  ".join(cells).rstrip()
        lines.append(term.paint(line, "bold") if term else line)
    for row in rows:
        cells = [pad(cell, widths[index], aligns[index]) for index, cell in enumerate(row)]
        lines.append(indent + "  ".join(cells).rstrip())
    return "\n".join(lines)


def bar(value: float, maximum: float, width: int = 30, filled: str = "█", empty: str = " ") -> str:
    if maximum <= 0:
        return empty * width
    count = int(round(width * (value / maximum)))
    count = max(0, min(width, count))
    if value > 0 and count == 0:
        count = 1
    return filled * count + empty * (width - count)


def die(message: str, code: int = 1):
    print("klok: %s" % message, file=sys.stderr)
    return code
