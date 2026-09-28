"""Audio metadata probing with mutagen, plus a manual WAV RIFF fallback.

Two things make this module longer than it looks:

1. **Keys are lowercased by mutagen** for Vorbis/FLAC/Opus/ASF, so every lookup
   must be case-insensitive.
2. **``mutagen.wave.WAVE(...).tags`` is ``None`` even when the file carries a
   real ``LIST/INFO`` ``ICRD`` chunk** (verified: ffmpeg writes
   ``b'ICRD\\x14\\x00\\x00\\x002018-01-02 03:04:05\\x00'`` and mutagen reports
   no tags).  Hence the hand-rolled RIFF walker.
"""

from __future__ import annotations

import logging
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from ..longpath import win_long
from ..timeparse import ParsedTime, combine_id3_parts, parse_datetime

log = logging.getLogger(__name__)


@dataclass
class AudioInfo:
    duration_ms: int | None = None
    codec: str | None = None
    container: str | None = None
    error: str | None = None


@dataclass
class AudioTime:
    parsed: ParsedTime | None = None
    source: str = "none"
    raw: str | None = None


# --------------------------------------------------------------------------
# extension -> mutagen class
# --------------------------------------------------------------------------

def _mutagen_classes():
    """Import lazily so a broken optional dependency cannot break the CLI."""
    import mutagen
    from mutagen import (
        aac, aiff, apev2, asf, dsf, flac, monkeysaudio, mp3, mp4, musepack,
        oggopus, oggspeex, oggvorbis, optimfrog, trueaudio, wave, wavpack,
    )

    return {
        "mp3": mp3.MP3, "mp2": mp3.MP3, "mpga": mp3.MP3,
        "aac": aac.AAC,
        "aiff": aiff.AIFF, "aif": aiff.AIFF, "aifc": aiff.AIFF,
        "flac": flac.FLAC,
        "ogg": oggvorbis.OggVorbis, "oga": oggvorbis.OggVorbis,
        "spx": oggspeex.OggSpeex,
        "opus": oggopus.OggOpus,
        "wma": asf.ASF, "asf": asf.ASF, "asx": asf.ASF,
        "wv": wavpack.WavPack,
        "ape": monkeysaudio.MonkeysAudio, "apl": monkeysaudio.MonkeysAudio,
        "mac": monkeysaudio.MonkeysAudio,
        "mpc": musepack.Musepack, "mp+": musepack.Musepack, "mpp": musepack.Musepack,
        "tta": trueaudio.TrueAudio,
        "ofr": optimfrog.OptimFROG, "ofs": optimfrog.OptimFROG,
        "wav": wave.WAVE, "wave": wave.WAVE,
        "dsf": dsf.DSF,
        "m4a": mp4.MP4, "m4b": mp4.MP4, "m4r": mp4.MP4,
        "m4p": mp4.MP4, "aax": mp4.MP4, "aa": mp4.MP4,
        "_fallback": mutagen.File,
    }


_APEV2_EXTS = {"ape", "apl", "mac", "wv", "tta", "mpc", "mp+", "mpp", "ofr", "ofs"}
_APEV2_KEYS = ("Year", "Date", "Record Date", "Record Year", "Original Year", "Year Recorded")


def _ext_of(path: str | Path) -> str:
    name = Path(path).name
    _, dot, ext = name.rpartition(".")
    return ext.lower() if dot else ""


def _lookup(tags, keys, *, case_insensitive: bool = True):
    """Fetch the first present key from a mutagen tag mapping."""
    if tags is None:
        return None, None
    try:
        items = dict(tags)
    except Exception:
        return None, None
    if case_insensitive:
        folded = {str(k).lower(): v for k, v in items.items()}
        for key in keys:
            hit = folded.get(key.lower())
            if hit is not None:
                return key, hit
        return None, None
    for key in keys:
        if key in items and items[key] is not None:
            return key, items[key]
    return None, None


def _value_to_str(value) -> str | None:
    """Flatten a mutagen tag value to a single displayable string."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple, bytes, str)):
        inner = _frame_text(value)
        if inner:
            value = inner
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value if v is not None]
        return parts[0] if parts else None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace").strip("\x00").strip()
        except Exception:
            return None
    text = str(value)
    return text or None


# --------------------------------------------------------------------------
# WAV RIFF LIST/INFO
# --------------------------------------------------------------------------

def riff_info_date(path: str | Path) -> str | None:
    """Read ``ICRD``/``IDIT`` from a WAV's ``LIST``/``INFO`` chunk.

    RIFF layout: ``RIFF<size>WAVE`` then chunks of ``<id><u32 size><data>``,
    each padded to an even length.  A ``LIST`` chunk whose form type is
    ``INFO`` holds sub-chunks with the same shape.
    """
    try:
        with open(win_long(path), "rb") as fh:
            head = fh.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return None
            while True:
                header = fh.read(8)
                if len(header) < 8:
                    return None
                chunk_id, size = struct.unpack("<4sI", header)
                if size == 0:
                    continue
                data = fh.read(size)
                if size % 2:
                    fh.read(1)  # pad byte
                if chunk_id == b"LIST" and data[:4] == b"INFO":
                    found = _parse_info_subchunks(data[4:])
                    if found:
                        return found
    except OSError as exc:
        log.debug("RIFF parse failed for %s: %s", path, exc)
    return None


def _parse_info_subchunks(blob: bytes) -> str | None:
    pos = 0
    while pos + 8 <= len(blob):
        cid, size = struct.unpack_from("<4sI", blob, pos)
        pos += 8
        if size > len(blob) - pos:
            return None
        raw = blob[pos:pos + size]
        pos += size + (size % 2)
        if cid in (b"ICRD", b"IDIT", b"ICMT", b"IENG"):
            text = raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
            if text and cid in (b"ICRD", b"IDIT"):
                return text
    return None


# --------------------------------------------------------------------------
# per-container chains
# --------------------------------------------------------------------------

def _from_id3(tags) -> AudioTime:
    if tags is None:
        return AudioTime()
    try:
        keys = [k for k in tags.keys()]
    except Exception:
        return AudioTime()

    def frame(*names):
        for name in names:
            if name in tags:
                try:
                    return tags[name]
                except Exception:
                    continue
        return None

    for name in ("TDRC", "TDRL", "TDOR", "TORY", "TDEN"):
        value = frame(name)
        if value is not None:
            parsed, raw = _parse_first(value)
            if parsed is not None:
                return AudioTime(parsed, "id3", raw)

    # Legacy ID3 2.3: TYER + TDAT + TIME
    combined = combine_id3_parts(
        _value_to_str(frame("TYER")),
        _value_to_str(frame("TDAT")),
        _value_to_str(frame("TIME")),
    )
    if combined is not None:
        return AudioTime(combined, "id3", "TYER/TDAT/TIME")

    # Some rippers put a bare date in a TXXX frame.
    for name in keys:
        if str(name).startswith("TXXX"):
            try:
                value = tags[name]
            except Exception:
                continue
            parsed, raw = _parse_first(value)
            if parsed is not None and _looks_like_date(raw or ""):
                return AudioTime(parsed, "id3", raw)
    return AudioTime()


def _looks_like_date(text: str) -> bool:
    return bool(re.match(r"^\s*\d{4}[-/:]?\d{1,2}", text or ""))


def _parse_first(value) -> tuple[ParsedTime | None, str | None]:
    for candidate in _iter_values(value):
        parsed = parse_datetime(candidate)
        if parsed is not None:
            return parsed, str(candidate)[:255]
    return None, None


def _frame_text(frame):
    """Unwrap a mutagen frame object to its list of text values.

    ``ID3`` frames carry their payload in ``.text`` (a list); ``MP4FreeForm``
    and friends expose ``.value`` or the frame itself.  Without this, a frame
    stringifies to ``TDRC(encoding=..., text=[...])`` and never parses.
    """
    if frame is None:
        return None
    for attr in ("text", "value", "values"):
        inner = getattr(frame, attr, None)
        if isinstance(inner, (list, tuple)) and inner:
            return [v for v in inner if v is not None]
        if inner is not None and not isinstance(inner, (list, tuple)):
            return [inner]
    return [frame]


def _iter_values(value):
    if value is None:
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_values(item)
        return
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:
            return
    if not isinstance(value, str):
        # A mutagen frame object: unwrap it.
        for item in _frame_text(value):
            yield from _iter_values(item)
        return
    yield value


def _from_vorbis_like(tags, source: str) -> AudioTime:
    _, value = _lookup(tags, ("originaldate", "originalyear", "date", "year"))
    parsed, raw = _parse_first(value)
    if parsed is not None:
        return AudioTime(parsed, source, raw)
    return AudioTime()


def _from_mp4(tags) -> AudioTime:
    if tags is None:
        return AudioTime()
    _, value = _lookup(tags, ("\xa9day", "purd", "©day"), case_insensitive=False)
    parsed, raw = _parse_first(value)
    if parsed is not None:
        return AudioTime(parsed, "m4a", raw)
    # Fall back to a case-insensitive scan for odd writers.
    _, value = _lookup(tags, ("day", "purd", "creationdate", "date"))
    parsed, raw = _parse_first(value)
    if parsed is not None:
        return AudioTime(parsed, "m4a", raw)
    return AudioTime()


def _from_asf(tags) -> AudioTime:
    _, value = _lookup(
        tags,
        ("WM/OriginalReleaseTime", "WM/OriginalReleaseYear", "WM/Year",
         "WM/EncodingTime", "WM/BeatsPerMinute"),
    )
    parsed, raw = _parse_first(value)
    if parsed is not None:
        return AudioTime(parsed, "asf", raw)
    _, value = _lookup(tags, ("date", "creationdate", "originaldate"))
    parsed, raw = _parse_first(value)
    if parsed is not None:
        return AudioTime(parsed, "asf", raw)
    return AudioTime()


def _from_apev2(tags) -> AudioTime:
    _, value = _lookup(
        tags,
        ("Year", "Date", "Record Date", "Record Year", "Original Year",
         "Recording Time", "Year Recorded"),
    )
    parsed, raw = _parse_first(value)
    if parsed is not None:
        return AudioTime(parsed, "apev2", raw)
    return AudioTime()


def _from_generic(tags) -> AudioTime:
    _, value = _lookup(
        tags,
        ("date", "originaldate", "originalyear", "year", "recordingdate",
         "creation_time", "creationdate", "wm/year", "wm/datecreated",
         "record_date", "releasedate"),
    )
    parsed, raw = _parse_first(value)
    if parsed is not None:
        return AudioTime(parsed, "mutagen_meta", raw)
    return AudioTime()


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def probe_audio_time(path: str | Path) -> AudioTime:
    """Extract the embedded recording time from an audio file."""
    ext = _ext_of(path)
    classes = _mutagen_classes()
    cls = classes.get(ext)

    if ext in _APEV2_EXTS:
        try:
            from mutagen.apev2 import APEv2File

            cls = APEv2File
        except Exception:
            cls = None

    tags = None
    if cls is not None:
        try:
            # win_long keeps long source paths openable; mutagen hands the
            # string straight to open(), which understands the \\?\ prefix.
            audio = cls(win_long(path))
            tags = getattr(audio, "tags", None)
        except Exception as exc:
            log.debug("mutagen open failed for %s: %s", path, exc)
            tags = None

    if ext in ("wav", "wave"):
        found = _from_id3(tags)
        if found.parsed is not None:
            return found
        raw = riff_info_date(path)
        if raw:
            parsed = parse_datetime(raw)
            if parsed is not None:
                return AudioTime(parsed, "riff_icrd", raw[:255])
        return AudioTime()

    if ext in ("mp3", "mp2", "mpga", "dsf"):
        return _from_id3(tags)
    if ext in ("m4a", "m4b", "m4r", "m4p", "aax", "aa"):
        return _from_mp4(tags)
    if ext in ("wma", "asf", "asx"):
        return _from_asf(tags)
    if ext in _APEV2_EXTS:
        return _from_apev2(tags)
    if ext in ("aiff", "aif", "aifc", "aac"):
        found = _from_id3(tags)
        if found.parsed is not None:
            return found
        return _from_generic(tags)
    if ext in ("mid", "midi", "rmi", "kar"):
        return AudioTime()
    # Vorbis-family containers (FLAC, Ogg Vorbis/Speex, Opus, Ogg FLAC).
    if ext in ("flac", "ogg", "oga", "opus", "spx", "ogx", "oggflac"):
        found = _from_vorbis_like(tags, "flac")
        if found.parsed is not None:
            return found
        return _from_generic(tags)

    return _from_generic(tags)


def probe_audio_info(path: str | Path) -> AudioInfo:
    """Duration and codec, used for the audio card overlay and player."""
    ext = _ext_of(path)
    classes = _mutagen_classes()
    cls = classes.get(ext)
    info = AudioInfo(container=ext.upper() or None)

    if cls is not None:
        try:
            audio = cls(win_long(path))
            length = getattr(audio, "info", None)
            if length is not None:
                duration = getattr(length, "length", None)
                if duration:
                    info.duration_ms = int(round(float(duration) * 1000))
                bitrate = getattr(length, "bitrate", None)
                if bitrate:
                    info.codec = f"{int(bitrate) // 1000} kbps"
                channels = getattr(length, "channels", None)
                rate = getattr(length, "sample_rate", None)
                if channels and rate:
                    info.codec = (info.codec + " / " if info.codec else "") + \
                        f"{channels}ch {int(rate) // 1000}kHz"
            return info
        except Exception as exc:
            info.error = str(exc)[:255]
    return info


__all__ = ["AudioInfo", "AudioTime", "probe_audio_time", "probe_audio_info", "riff_info_date"]
