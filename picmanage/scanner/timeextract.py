"""The embedded-time extraction chain.

This module is the correctness core of the whole project: everything
downstream -- dedup winner selection, the ``YYYY/YYYY-MM`` folder a file lands
in, and what the web gallery shows -- depends on the value produced here.

The rule that matters: **a file's mtime is when it was copied, not when it was
created.**  It is used only when the operator explicitly opts in with
``--mtime-fallback``, and is then labelled so the UI can say so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import MEDIA_TYPE_LABELS
from ..timeparse import ParsedTime
from .probe_audio import probe_audio_time
from .probe_image import probe_image_time
from .probe_video import probe_video_time

log = logging.getLogger(__name__)

#: Sources that are a real, encoder-written timestamp.  Everything else is a
#: guess and loses any dedup comparison against these.
REAL_SOURCES = frozenset({
    "exif", "xmp", "quicktime", "quicktime_apple", "ffprobe_stream",
    "ffprobe_meta", "id3", "dsf_id3", "m4a", "flac", "asf", "apev2",
    "riff_icrd", "mutagen_meta", "manual",
})


@dataclass(frozen=True)
class TimeInfo:
    """The outcome of time extraction for one file."""

    taken_at: datetime | None
    time_source: str
    time_raw: str | None = None
    time_precision: str = "none"

    @property
    def is_real(self) -> bool:
        """True when the timestamp came from inside the file."""
        return self.time_source in REAL_SOURCES

    @property
    def year(self) -> int | None:
        return self.taken_at.year if self.taken_at else None

    @property
    def month(self) -> int | None:
        return self.taken_at.month if self.taken_at else None


NONE = TimeInfo(None, "none", None, "none")


def _from_parsed(parsed: ParsedTime | None, source: str, raw: str | None) -> TimeInfo:
    if parsed is None:
        return NONE
    dt = parsed.to_local_naive()
    return TimeInfo(dt, source, (raw or "")[:255], parsed.precision)


def extract_time(
    path: str | Path,
    media_type: str,
    *,
    mtime: float | None = None,
    mtime_fallback: bool = False,
) -> TimeInfo:
    """Run the extraction chain for one file.

    Order: embedded image metadata -> embedded container metadata -> embedded
    audio tags -> (opt-in) filesystem mtime -> ``none``.
    """
    try:
        if media_type == "image":
            found = probe_image_time(path)
            if found.parsed is not None:
                return _from_parsed(found.parsed, found.source, found.raw)

        elif media_type == "video":
            found = probe_video_time(path)
            if found.parsed is not None:
                return _from_parsed(found.parsed, found.source, found.raw)
            # A video with no container time may still carry an audio tag.
            found_audio = probe_audio_time(path)
            if found_audio.parsed is not None:
                return _from_parsed(found_audio.parsed, found_audio.source, found_audio.raw)

        elif media_type == "audio":
            found = probe_audio_time(path)
            if found.parsed is not None:
                return _from_parsed(found.parsed, found.source, found.raw)
            # An audio-only container ffprobe may still know something.
            found_video = probe_video_time(path)
            if found_video.parsed is not None:
                return _from_parsed(found_video.parsed, found_video.source, found_video.raw)

    except Exception as exc:  # never let one bad file abort a scan
        log.debug("time extraction failed for %s: %s", path, exc)

    if mtime_fallback and mtime is not None:
        try:
            return TimeInfo(
                datetime.fromtimestamp(mtime), "mtime", None, "second"
            )
        except (OSError, OverflowError, ValueError):
            pass

    return NONE


def bucket_for(info: TimeInfo, min_precision: str = "year") -> tuple[str | None, int | None]:
    """Decide the library bucket: ``(relative_dir, taken_year)``.

    Returns ``(None, None)`` when the file has no usable time and must go to
    ``_unsorted/``.

    A time that came from ``--mtime-fallback`` *is* filed here: the operator
    explicitly asked for that behaviour, and ``info.time_source`` carries the
    provenance so the UI can badge it as a guess.
    """
    if info.taken_at is None:
        return None, None
    year = info.taken_at.year
    month = info.taken_at.month
    if min_precision == "month" and info.time_precision == "year":
        # Only the year is known: too coarse to place in a month folder.
        return None, None
    if min_precision == "day" and info.time_precision in ("year", "month"):
        return None, None
    return f"{year:04d}/{year:04d}-{month:02d}", year


def describe(info: TimeInfo) -> str:
    """Human-readable provenance for the UI badge."""
    from ..config import PRECISION_LABELS

    if info.time_source == "none":
        return "未找到内嵌时间"
    label = PRECISION_LABELS.get(info.time_precision, info.time_precision)
    return label


__all__ = [
    "TimeInfo", "NONE", "REAL_SOURCES", "extract_time", "bucket_for",
    "describe", "MEDIA_TYPE_LABELS",
]
