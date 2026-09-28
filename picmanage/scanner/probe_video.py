"""Video metadata probing via ffprobe.

ffprobe reports ``creation_time`` in **UTC** (``...Z``), which must be converted
to local wall-clock before it is stored -- otherwise every video lands in the
wrong month for anyone east of Greenwich.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import FFPROBE_TIMEOUT
from ..longpath import win_long
from ..timeparse import ParsedTime, parse_datetime

log = logging.getLogger(__name__)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Tag keys tried in order, with the time_source each one implies.
# (key, source, expect_utc)
_TAG_CHAIN = (
    ("creation_time", "quicktime", True),
    ("com.apple.quicktime.creationdate", "quicktime_apple", True),
    ("com.apple.quicktime.creation_date", "quicktime_apple", True),
    ("creationdate", "ffprobe_meta", False),
    ("originaldate", "ffprobe_meta", False),
    ("original_date", "ffprobe_meta", False),
    ("date", "ffprobe_meta", False),
    ("record_date", "ffprobe_meta", False),
    ("encoded_date", "ffprobe_meta", False),
    ("icrd", "ffprobe_meta", False),
    ("wm/originalreleasetime", "ffprobe_meta", True),
    ("wm/encodingtime", "ffprobe_meta", True),
    ("wm/year", "ffprobe_meta", False),
    ("wm/datecreated", "ffprobe_meta", True),
)

_STREAM_TAG_CHAIN = (
    ("creation_time", "ffprobe_stream", True),
    ("date", "ffprobe_stream", False),
    ("originaldate", "ffprobe_stream", False),
)


@dataclass
class VideoInfo:
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    container: str | None = None
    thumb_ready: int = 0
    error: str | None = None


@dataclass
class VideoTime:
    parsed: ParsedTime | None = None
    source: str = "none"
    raw: str | None = None


def _run_ffprobe(path: str | Path) -> dict | None:
    # NOTE: ``-nostdin`` is deliberately not used.  Some ffmpeg builds (this
    # one included) do not accept it and fail the whole invocation; passing
    # ``stdin=DEVNULL`` below achieves the same thing portably.
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        "-show_entries",
        "format=format_name,duration,size,bit_rate,tags:"
        "stream=index,codec_type,codec_name,width,height,duration,tags",
        win_long(path) if os.name == "nt" else str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=FFPROBE_TIMEOUT,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        log.debug("ffprobe timeout for %s", path)
        return None
    except (OSError, ValueError) as exc:
        log.debug("ffprobe unavailable: %s", exc)
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _fold_tags(tags) -> dict[str, str]:
    """Lower-case the tag keys so lookups are case-insensitive."""
    if not isinstance(tags, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in tags.items():
        if value is None:
            continue
        out[str(key).lower()] = str(value)
    return out


def _to_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def probe_video_time(path: str | Path) -> VideoTime:
    data = _run_ffprobe(path)
    if not data:
        return VideoTime()

    fmt = data.get("format") or {}
    streams = data.get("streams") or []

    for key, source, expect_utc in _TAG_CHAIN:
        value = _fold_tags(fmt.get("tags")).get(key)
        if not value:
            continue
        parsed = parse_datetime(value)
        if parsed is None:
            continue
        # An explicit Z/offset is authoritative; otherwise honour the table.
        if parsed.is_utc or expect_utc:
            parsed = ParsedTime(parsed.to_local_naive(), parsed.precision)
        return VideoTime(parsed, source, value[:255])

    # Video streams first, then audio, then anything else.
    ordered = sorted(
        streams,
        key=lambda s: {"video": 0, "audio": 1}.get(str(s.get("codec_type")), 2),
    )
    for stream in ordered:
        tags = _fold_tags(stream.get("tags"))
        for key, source, expect_utc in _STREAM_TAG_CHAIN:
            value = tags.get(key)
            if not value:
                continue
            parsed = parse_datetime(value)
            if parsed is None:
                continue
            if parsed.is_utc or expect_utc:
                parsed = ParsedTime(parsed.to_local_naive(), parsed.precision)
            return VideoTime(parsed, source, value[:255])

    return VideoTime()


def probe_video_info(path: str | Path) -> VideoInfo:
    data = _run_ffprobe(path)
    if not data:
        return VideoInfo(error="ffprobe failed", thumb_ready=0)

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    info = VideoInfo(container=fmt.get("format_name") or None)

    duration = _to_float(fmt.get("duration"))
    if duration is None:
        for stream in streams:
            duration = _to_float(stream.get("duration"))
            if duration:
                break
    if duration:
        info.duration_ms = int(round(duration * 1000))

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video:
        info.width = _int_or_none(video.get("width"))
        info.height = _int_or_none(video.get("height"))
        info.codec = video.get("codec_name")
        info.thumb_ready = 1
    else:
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if audio:
            info.codec = audio.get("codec_name")
    return info


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ffprobe_available() -> bool:
    try:
        proc = subprocess.run(
            ["ffprobe", "-version"],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


__all__ = [
    "VideoInfo", "VideoTime", "probe_video_time", "probe_video_info",
    "ffprobe_available",
]
