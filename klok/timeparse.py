"""Parsing and formatting of the times, durations and ranges klok accepts.

The goal is that anything a human would plausibly type at a prompt works:
``9am``, ``-15min``, ``yesterday``, ``last monday``, ``:lastweek``,
``2026-08-31 09:00 to 11:30``.
"""

import re
from datetime import date, datetime, timedelta

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_UNITS = {
    "s": "seconds", "sec": "seconds", "secs": "seconds",
    "second": "seconds", "seconds": "seconds",
    "m": "minutes", "min": "minutes", "mins": "minutes",
    "minute": "minutes", "minutes": "minutes",
    "h": "hours", "hr": "hours", "hrs": "hours",
    "hour": "hours", "hours": "hours",
    "d": "days", "day": "days", "days": "days",
    "w": "weeks", "week": "weeks", "weeks": "weeks",
}

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_AMPM_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?\s*([ap])\.?m\.?$", re.I)
_DATE_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")
_SHORTDATE_RE = re.compile(r"^(\d{1,2})[-/](\d{1,2})$")
_REL_RE = re.compile(r"^([+-])\s*(.+)$")
_AGO_RE = re.compile(r"^(.*?)\s+ago$", re.I)
_LAST_N_RE = re.compile(r"^last\s+(\d+)\s+(day|days|week|weeks|month|months|year|years)$", re.I)


class TimeParseError(ValueError):
    """Raised when user input cannot be understood as a time or duration."""


def local_tz():
    return datetime.now().astimezone().tzinfo


def now_local() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def to_iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def from_iso(text: str) -> datetime:
    """Parse an ISO timestamp, assuming local time when no offset is given."""
    if isinstance(text, datetime):
        return text if text.tzinfo else text.replace(tzinfo=local_tz())
    raw = str(text).strip().replace("Z", "+00:00")
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=local_tz())
    return value.replace(microsecond=0)


# ---------------------------------------------------------------- durations
def parse_duration(text) -> timedelta:
    """``90`` (minutes), ``1h30m``, ``1:30``, ``45s``, ``2 hours``."""
    if isinstance(text, timedelta):
        return text
    raw = str(text).strip().lower()
    if not raw:
        raise TimeParseError("empty duration")
    negative = raw.startswith("-")
    raw = raw.lstrip("+-").strip()

    clock = _CLOCK_RE.match(raw)
    if clock:
        hours, minutes, seconds = clock.groups()
        delta = timedelta(hours=int(hours), minutes=int(minutes), seconds=int(seconds or 0))
        return -delta if negative else delta

    if re.fullmatch(r"\d+(\.\d+)?", raw):
        delta = timedelta(minutes=float(raw))
        return -delta if negative else delta

    matches = _DURATION_RE.findall(raw)
    if not matches or _DURATION_RE.sub("", raw).strip(" ,and"):
        raise TimeParseError("cannot parse duration: %r" % text)
    kwargs = {}
    for amount, unit in matches:
        key = _UNITS.get(unit)
        if key is None:
            raise TimeParseError("unknown time unit %r in %r" % (unit, text))
        kwargs[key] = kwargs.get(key, 0) + float(amount)
    delta = timedelta(**kwargs)
    return -delta if negative else delta


def format_duration(delta: timedelta, style: str = "short") -> str:
    """``short`` -> ``1h 30m``; ``clock`` -> ``01:30:00``."""
    total = int(round(delta.total_seconds()))
    sign = "-" if total < 0 else ""
    total = abs(total)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if style == "clock":
        return "%s%02d:%02d:%02d" % (sign, hours, minutes, seconds)
    if style == "hours":
        return "%s%.2f" % (sign, total / 3600.0)
    if hours:
        return "%s%dh %02dm" % (sign, hours, minutes)
    if minutes:
        return "%s%dm" % (sign, minutes) if not seconds else "%s%dm %02ds" % (sign, minutes, seconds)
    return "%s%ds" % (sign, seconds)


def round_duration(delta: timedelta, minutes: int) -> timedelta:
    """Round up to the next multiple of ``minutes`` (0 disables rounding)."""
    if minutes <= 0:
        return delta
    step = minutes * 60
    seconds = delta.total_seconds()
    if seconds <= 0:
        return timedelta(0)
    return timedelta(seconds=step * -(-int(seconds) // step))


# ---------------------------------------------------------------- datetimes
def _at(reference: datetime, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return reference.replace(hour=hour, minute=minute, second=second, microsecond=0)


def _weekday_before(reference: datetime, weekday: int, weeks_back: int = 0) -> datetime:
    midnight = _at(reference, 0)
    delta = (midnight.weekday() - weekday) % 7
    if delta == 0:
        delta = 0 if weeks_back == 0 else 0
    return midnight - timedelta(days=delta + 7 * weeks_back)


def parse_datetime_ex(text, now: datetime = None):
    """Return ``(datetime, granularity)`` where granularity is ``day`` or ``instant``."""
    now = now or now_local()
    if isinstance(text, datetime):
        return (text if text.tzinfo else text.replace(tzinfo=now.tzinfo)), "instant"
    raw = " ".join(str(text).strip().split())
    if not raw:
        raise TimeParseError("empty time")
    low = raw.lower()

    if low in ("now",):
        return now, "instant"
    if low in ("today", "tod"):
        return _at(now, 0), "day"
    if low in ("yesterday", "yest", "yd"):
        return _at(now, 0) - timedelta(days=1), "day"
    if low == "tomorrow":
        return _at(now, 0) + timedelta(days=1), "day"
    if low == "noon":
        return _at(now, 12), "instant"
    if low == "midnight":
        return _at(now, 0), "instant"

    ago = _AGO_RE.match(low)
    if ago:
        return now - parse_duration(ago.group(1)), "instant"

    rel = _REL_RE.match(low)
    if rel and not _DATE_RE.match(low):
        delta = parse_duration(rel.group(2))
        return (now - delta if rel.group(1) == "-" else now + delta), "instant"

    words = low.split()
    if words[0] in ("last", "this") and len(words) == 2 and words[1] in WEEKDAYS:
        return _weekday_before(now, WEEKDAYS[words[1]], 1 if words[0] == "last" else 0), "day"
    if low in WEEKDAYS:
        return _weekday_before(now, WEEKDAYS[low]), "day"

    # A date and a time, in either "T" or space separated form.
    if len(words) == 2 or "t" in low:
        for splitter in (" ", "T", "t"):
            if splitter == " " and len(words) != 2:
                continue
            head, sep, tail = raw.partition(splitter)
            if not sep or not tail:
                continue
            try:
                day, _ = parse_datetime_ex(head, now)
                clock, _ = _parse_clock(tail, day)
            except TimeParseError:
                continue
            return clock, "instant"

    clock = _parse_clock_or_none(raw, now)
    if clock is not None:
        return clock, "instant"

    match = _DATE_RE.match(low)
    if match:
        year, month, day = (int(part) for part in match.groups())
        return now.replace(year=year, month=month, day=day, hour=0, minute=0, second=0, microsecond=0), "day"

    match = _SHORTDATE_RE.match(low)
    if match:
        month, day = (int(part) for part in match.groups())
        return now.replace(month=month, day=day, hour=0, minute=0, second=0, microsecond=0), "day"

    if re.fullmatch(r"\d{10}", low):
        return datetime.fromtimestamp(int(low), now.tzinfo), "instant"

    try:
        return from_iso(raw), "instant"
    except ValueError:
        raise TimeParseError("cannot parse time: %r" % text)


def _parse_clock(text: str, reference: datetime):
    result = _parse_clock_or_none(text, reference)
    if result is None:
        raise TimeParseError("cannot parse time of day: %r" % text)
    return result, "instant"


def _parse_clock_or_none(text: str, reference: datetime):
    raw = text.strip()
    match = _AMPM_RE.match(raw)
    if match:
        hour = int(match.group(1)) % 12
        if match.group(4).lower() == "p":
            hour += 12
        return _at(reference, hour, int(match.group(2) or 0), int(match.group(3) or 0))
    match = _CLOCK_RE.match(raw)
    if match:
        hour, minute, second = match.groups()
        if int(hour) > 23:
            return None
        return _at(reference, int(hour), int(minute), int(second or 0))
    if re.fullmatch(r"\d{1,2}h", raw, re.I):
        return _at(reference, int(raw[:-1]))
    return None


def parse_datetime(text, now: datetime = None) -> datetime:
    return parse_datetime_ex(text, now)[0]


# ------------------------------------------------------------------- ranges
def day_bounds(reference: datetime):
    start = _at(reference, 0)
    return start, start + timedelta(days=1)


def week_bounds(reference: datetime, week_start: str = "monday"):
    midnight = _at(reference, 0)
    offset = midnight.weekday() if week_start.lower() == "monday" else (midnight.weekday() + 1) % 7
    start = midnight - timedelta(days=offset)
    return start, start + timedelta(days=7)


def month_bounds(reference: datetime):
    start = _at(reference, 0).replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def quarter_bounds(reference: datetime):
    first_month = 3 * ((reference.month - 1) // 3) + 1
    start = _at(reference, 0).replace(month=first_month, day=1)
    end = start
    for _ in range(3):
        end = (end + timedelta(days=32)).replace(day=1)
    return start, end


def year_bounds(reference: datetime):
    start = _at(reference, 0).replace(month=1, day=1)
    return start, start.replace(year=start.year + 1)


def _shift_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                          31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return value.replace(year=year, month=month, day=day)


EPOCH_FALLBACK = datetime(1970, 1, 1)


def parse_range(tokens, now: datetime = None, week_start: str = "monday", default=None):
    """Turn range words into a half-open ``(start, end)`` pair.

    Accepts Timewarrior-style hints (``:week``), plain dates, ``A to B``
    spans, ``since monday`` and ``last 7 days``.
    """
    now = now or now_local()
    if isinstance(tokens, str):
        tokens = tokens.split()
    tokens = [str(token) for token in (tokens or []) if str(token).strip()]
    if not tokens:
        if default == "all" or default is None:
            return EPOCH_FALLBACK.replace(tzinfo=now.tzinfo), now + timedelta(days=1)
        return parse_range([default], now, week_start)

    joined = " ".join(tokens).strip()
    low = joined.lower()

    hint = low.lstrip(":")
    if low.startswith(":") or hint in ("all",):
        return _hint_range(hint, now, week_start)

    match = _LAST_N_RE.match(low)
    if match:
        count, unit = int(match.group(1)), match.group(2).rstrip("s")
        end = _at(now, 0) + timedelta(days=1)
        if unit == "day":
            return end - timedelta(days=count), end
        if unit == "week":
            return end - timedelta(weeks=count), end
        if unit == "month":
            return _shift_months(end, -count), end
        return _shift_months(end, -12 * count), end

    if low.startswith("since "):
        start, _ = parse_datetime_ex(joined[6:], now)
        return start, now + timedelta(seconds=1)

    for separator in (" to ", " - ", "..", " until "):
        if separator in low:
            index = low.index(separator)
            head = joined[:index].strip()
            tail = joined[index + len(separator):].strip()
            start, _ = parse_datetime_ex(head, now)
            end, end_kind = parse_datetime_ex(tail, now)
            if end_kind == "day":
                end += timedelta(days=1)
            return start, end

    value, kind = parse_datetime_ex(joined, now)
    if kind == "day":
        return value, value + timedelta(days=1)
    return value, now + timedelta(seconds=1)


def _hint_range(hint: str, now: datetime, week_start: str):
    hint = hint.lower()
    if hint in ("day", "today", "tod"):
        return day_bounds(now)
    if hint in ("yesterday", "yest"):
        return day_bounds(now - timedelta(days=1))
    if hint == "week":
        return week_bounds(now, week_start)
    if hint in ("lastweek", "last-week"):
        start, end = week_bounds(now - timedelta(days=7), week_start)
        return start, end
    if hint == "month":
        return month_bounds(now)
    if hint in ("lastmonth", "last-month"):
        return month_bounds(_shift_months(_at(now, 0).replace(day=1), -1))
    if hint == "quarter":
        return quarter_bounds(now)
    if hint == "year":
        return year_bounds(now)
    if hint in ("lastyear", "last-year"):
        return year_bounds(now.replace(year=now.year - 1))
    if hint == "all":
        return EPOCH_FALLBACK.replace(tzinfo=now.tzinfo), now + timedelta(days=1)
    match = re.fullmatch(r"(\d+)d(ays)?", hint)
    if match:
        end = _at(now, 0) + timedelta(days=1)
        return end - timedelta(days=int(match.group(1))), end
    if hint in WEEKDAYS:
        start = _weekday_before(now, WEEKDAYS[hint])
        return start, start + timedelta(days=1)
    raise TimeParseError("unknown range hint: :%s" % hint)


def parse_hours_window(text: str):
    """``09:00-18:00`` -> ``(time, time)``; empty input -> ``None``."""
    if not text or "-" not in text:
        return None
    head, _, tail = text.partition("-")
    reference = now_local()
    start = _parse_clock_or_none(head.strip(), reference)
    end = _parse_clock_or_none(tail.strip(), reference)
    if start is None or end is None:
        return None
    return start.time(), end.time()


def format_time(value: datetime, time_format: str = "24") -> str:
    return value.strftime("%I:%M %p" if str(time_format) == "12" else "%H:%M")


def format_datetime(value: datetime, time_format: str = "24") -> str:
    return "%s %s" % (value.strftime("%Y-%m-%d"), format_time(value, time_format))


def humanize_ago(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now" if seconds < 5 else "%d seconds ago" % seconds
    if seconds < 3600:
        return "%d minute%s ago" % (seconds // 60, "" if seconds // 60 == 1 else "s")
    if seconds < 86400:
        return "%d hour%s ago" % (seconds // 3600, "" if seconds // 3600 == 1 else "s")
    return "%d day%s ago" % (seconds // 86400, "" if seconds // 86400 == 1 else "s")


def date_key(value: datetime) -> date:
    return value.date()
