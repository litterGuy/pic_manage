"""Parsing of the many date-string dialects found in media metadata.

Pure functions, no I/O: the whole module is unit-testable without fixtures.

Every function returns a :class:`ParsedTime` carrying

* ``dt``          -- naive ``datetime`` (local wall clock),
* ``precision``   -- ``second`` / ``minute`` / ``day`` / ``month`` / ``year``,
* ``is_utc``      -- whether the source string was explicitly UTC (``Z`` /
                     offset), in which case the caller must convert to local.

Two conventions coexist in the wild and must be kept apart:

* EXIF / XMP / ID3 carry **local wall-clock** time with no zone.  The value is
  stored verbatim -- a camera in UTC+8 that says ``2021:03:04 08:09:10`` means
  that clock reading, not ``00:09:10Z``.
* QuickTime / ffprobe carry **UTC** (``...Z``) and must be converted to local.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

PRECISIONS = ("second", "minute", "day", "month", "year")

_MONTHS = 12
_DAYS = 31
_HOURS = 23
_MINUTES = 59
_SECONDS = 59


@dataclass(frozen=True)
class ParsedTime:
    dt: datetime
    precision: str
    is_utc: bool = False

    def to_local_naive(self) -> datetime:
        """Convert an explicitly-UTC value to naive local wall clock."""
        if not self.is_utc:
            return self.dt
        aware = self.dt.replace(tzinfo=timezone.utc)
        return aware.astimezone().replace(tzinfo=None)


@dataclass(frozen=True)
class _Pattern:
    regex: re.Pattern[str]
    precision: str
    order: str  # "ymd" or "mdy"


# YYYY[:/-]MM[:/-]DD[ T]HH:MM[:SS][.fff][Z|+HH:MM|+HHMM]
_RE_DATETIME = _Pattern(
    re.compile(
        r"^(?P<y>\d{4})[-/:](?P<mo>\d{1,2})[-/:](?P<d>\d{1,2})"
        r"[ T]+(?P<h>\d{1,2}):(?P<mi>\d{1,2})(?::(?P<s>\d{1,2}))?"
        r"(?:\.(?P<frac>\d{1,9}))?"
        r"(?P<zone>Z|[+-]\d{2}:?\d{2}|[+-]\d{2})?$",
        re.IGNORECASE,
    ),
    "second",
    "ymd",
)

# YYYY-MM-DD  (day precision)
_RE_DATE = _Pattern(
    re.compile(r"^(?P<y>\d{4})[-/:](?P<mo>\d{1,2})[-/:](?P<d>\d{1,2})$"),
    "day",
    "ymd",
)

# YYYY-MM  (month precision)
_RE_MONTH = _Pattern(re.compile(r"^(?P<y>\d{4})[-/:](?P<mo>\d{1,2})$"), "month", "ymd")

# YYYY  (year precision)
_RE_YEAR = _Pattern(re.compile(r"^(?P<y>\d{4})$"), "year", "ymd")

# YYYYMMDDHHMMSS and shorter dense forms
_RE_COMPACT = _Pattern(
    re.compile(r"^(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})"
               r"(?:(?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})?)?$"),
    "second",
    "ymd",
)

# YYYYMM  (dense month)
_RE_COMPACT_MONTH = _Pattern(
    re.compile(r"^(?P<y>\d{4})(?P<mo>\d{2})$"), "month", "ymd"
)

# DD/MM/YYYY or MM/DD/YYYY with optional time -- ambiguous, handled separately
_RE_SLASHED = _Pattern(
    re.compile(
        r"^(?P<a>\d{1,2})/(?P<b>\d{1,2})/(?P<y>\d{4})"
        r"(?:[ T]+(?P<h>\d{1,2}):(?P<mi>\d{1,2})(?::(?P<s>\d{1,2}))?)?$"
    ),
    "second",
    "mdy",
)

_PATTERNS = (
    _RE_DATETIME, _RE_DATE, _RE_MONTH, _RE_YEAR,
    _RE_COMPACT, _RE_COMPACT_MONTH, _RE_SLASHED,
)


def _zone_info(zone: str | None) -> tuple[bool, timedelta | None]:
    """Return ``(is_utc, fixed_offset)`` for a matched zone suffix."""
    if not zone:
        return False, None
    if zone.upper() == "Z":
        return True, timedelta(0)
    sign = 1 if zone[0] == "+" else -1
    body = zone[1:].replace(":", "")
    if len(body) == 2:
        hours, minutes = int(body), 0
    elif len(body) == 4:
        hours, minutes = int(body[:2]), int(body[2:])
    else:
        return False, None
    return False, sign * timedelta(hours=hours, minutes=minutes)


def _build(y: int, mo: int, d: int, h: int, mi: int, s: int, frac: str | None) -> datetime | None:
    """Construct a datetime, clamping impossible components rather than failing.

    Media metadata is routinely wrong: day 32, hour 25.  A wrong-but-plausible
    date is far more useful to the user than a dropped file, so day and time
    components are clamped to the nearest legal value.

    A month outside 1..12 is different: there is no month to file the photo
    under, so it is rejected outright and the caller falls through to the next
    source (ultimately ``_unsorted``).
    """
    if not (1 <= y <= 9999):
        return None
    if not (1 <= mo <= 12):
        return None
    d = max(1, min(_DAYS, d))
    h = max(0, min(_HOURS, h))
    mi = max(0, min(_MINUTES, mi))
    s = max(0, min(_SECONDS, s))
    micro = 0
    if frac:
        micro = int((frac + "000000")[:6])
    try:
        return datetime(y, mo, d, h, mi, s, micro)
    except ValueError:  # e.g. 2021-02-30
        try:
            return datetime(y, mo, 1, h, mi, s, micro)
        except ValueError:
            return None


def parse_datetime(value: object) -> ParsedTime | None:
    """Parse any known date-string dialect.

    Returns ``None`` for anything unrecognised -- callers must then fall back
    to the next source in their chain rather than guessing.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return ParsedTime(value.replace(tzinfo=None), "second")
    if isinstance(value, (bytes, bytearray)):
        # Regex captures and some tag readers hand us raw bytes.
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            value = bytes(value).decode("utf-8", "replace")
    if not isinstance(value, str):
        # mutagen ID3TimeStamp and friends stringify usefully.
        try:
            value = str(value)
        except Exception:
            return None

    text = value.strip().strip('"').strip("'").strip()
    if not text:
        return None

    for pat in _PATTERNS:
        m = pat.regex.match(text)
        if not m:
            continue
        gd = m.groupdict()

        # Patterns have heterogeneous group sets (year-only has no month/day),
        # so every optional component is read with a default rather than by
        # direct indexing.
        try:
            if pat.order == "mdy":
                a, b = int(gd["a"]), int(gd["b"])
                # Prefer DD/MM/YYYY (the majority convention for camera
                # firmware); fall back to MM/DD when that cannot be a day.
                if a > 12 >= b:
                    d, mo = a, b
                elif b > 12 >= a:
                    mo, d = a, b
                else:
                    d, mo = a, b
                y = int(gd["y"])
            else:
                y = int(gd["y"])
                mo = int(gd.get("mo") or 1)
                d = int(gd.get("d") or 1)
        except (KeyError, TypeError, ValueError):
            continue

        h = int(gd.get("h") or 0)
        mi = int(gd.get("mi") or 0)
        s = int(gd.get("s") or 0)

        is_utc, offset = _zone_info(gd.get("zone"))
        dt = _build(y, mo, d, h, mi, s, gd.get("frac"))
        if dt is None:
            continue

        if offset is not None:
            # Explicit non-UTC offset: normalise to UTC so the caller's local
            # conversion produces the right instant.
            dt = (dt.replace(tzinfo=timezone(offset))
                  .astimezone(timezone.utc)
                  .replace(tzinfo=None))
            is_utc = True

        precision = pat.precision
        if precision == "second" and not (h or mi or s):
            # "2019-03-15 00:00:00" is a date-only value wearing a time.
            precision = "day"
        if precision == "second" and gd.get("mi") and not gd.get("s"):
            precision = "minute"

        return ParsedTime(dt, precision, is_utc)

    return None


def parse_date_only(value: object) -> datetime | None:
    """Parse a value that must denote a calendar day (no time component)."""
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return parsed.dt.replace(hour=0, minute=0, second=0, microsecond=0)


def combine_id3_parts(year: object, date: object, time: object) -> ParsedTime | None:
    """Join legacy ID3 2.3 ``TYER``/``TDAT``/``TIME`` frames.

    ``TYER`` is a bare year, ``TDAT`` is ``DDMM``, ``TIME`` is ``HHMM``.
    """
    try:
        y = int(str(year).strip())
    except (TypeError, ValueError):
        return None
    d = m = 1
    h = mi = 0
    if date:
        s = re.sub(r"\D", "", str(date))
        if len(s) == 4:
            d, m = int(s[:2]), int(s[2:])
    if time:
        s = re.sub(r"\D", "", str(time))
        if len(s) >= 4:
            h, mi = int(s[:2]), int(s[2:4])
    dt = _build(y, m, d, h, mi, 0, None)
    if dt is None:
        return None
    precision = "year" if (m == 1 and d == 1 and not (h or mi)) else "day"
    if h or mi:
        precision = "minute"
    return ParsedTime(dt, precision)


def iso(dt: datetime | None) -> str | None:
    """Render a naive datetime the way it is stored in SQLite."""
    if dt is None:
        return None
    return dt.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def from_iso(text: str | None) -> datetime | None:
    """Inverse of :func:`iso`; tolerates ISO-8601 variants too."""
    if not text:
        return None
    text = text.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M", "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


__all__ = ["ParsedTime", "parse_datetime", "parse_date_only", "combine_id3_parts", "iso", "from_iso"]
