"""Classification of a filesystem entry: media type, junk, name sanitising."""

from __future__ import annotations

import fnmatch
import os
import re
import unicodedata
from pathlib import Path

from ..config import (
    CONTROL_CHARS,
    EXT_TO_TYPE,
    ILLEGAL_CHARS,
    INDEXABLE_EXTS,
    JUNK_GLOBS,
    JUNK_NAMES,
    JUNK_SUFFIXES,
    RESERVED_NAMES,
    STEM_MAX_CHARS,
)


def split_ext(name: str) -> tuple[str, str]:
    """``('photo', 'jpg')`` -- case-folded extension, no dot.

    ``os.path.splitext`` is not used because it mishandles names like
    ``archive.tar.gz`` for our purposes and is locale-sensitive on some
    platforms.
    """
    base, dot, ext = name.rpartition(".")
    if not dot or not base:
        return name, ""
    return base, ext.lower()


def media_type_for(name: str) -> str:
    """``image`` / ``video`` / ``audio`` / ``other`` for a filename."""
    _, ext = split_ext(name)
    if not ext:
        return "other"
    return EXT_TO_TYPE.get(ext, "other")


def is_indexable(name: str) -> bool:
    _, ext = split_ext(name)
    return bool(ext) and ext in INDEXABLE_EXTS


def is_junk_name(name: str) -> bool:
    """True for OS/editor droppings that must never be indexed."""
    low = name.lower()
    if low in JUNK_NAMES:
        return True
    if low.startswith(JUNK_SUFFIXES):
        return True
    if low.endswith(".part") or low.endswith(".crdownload"):
        return True
    for pattern in JUNK_GLOBS:
        if fnmatch.fnmatch(low, pattern):
            return True
    # AppleDouble sidecars: "._IMG_0001.jpg"
    if low.startswith("._"):
        return True
    return False


def is_hidden(name: str) -> bool:
    """True when the *name* marks the entry as hidden.

    This is the dotfile convention, which is the only one available on POSIX
    and the only one usable when we have nothing but a name.
    """
    return name.startswith(".")


#: ``FILE_ATTRIBUTE_HIDDEN`` -- the attribute Explorer and ``attrib`` set.
WINDOWS_HIDDEN_ATTR = 0x2


def stat_is_hidden(stat: os.stat_result | None) -> bool:
    """True when a stat we already have carries the Windows hidden attribute.

    ``os.stat_result`` exposes ``st_file_attributes`` on Windows, so this costs
    no extra syscall on top of the ``stat`` the walker already performs.  A file
    can be hidden by attribute while its name has no leading dot, and ignoring
    that silently indexes files the operator expected to be skipped.
    """
    if stat is None or os.name != "nt":
        return False
    return bool(getattr(stat, "st_file_attributes", 0) & WINDOWS_HIDDEN_ATTR)


def path_is_hidden(path: Path) -> bool:
    """True when the entry itself or any component is a dot-entry."""
    for part in Path(path).parts:
        if part.startswith(".") and part not in (".", ".."):
            return True
    return False


_WS_RUN = re.compile(r"\s+")
_TRAILING = re.compile(r"^[\s.]+|[\s.]+$")


def sanitize_stem(stem: str, max_chars: int = STEM_MAX_CHARS) -> str:
    """Make a filename stem safe on Windows while preserving Unicode text.

    CJK and accented characters survive verbatim -- they are the user's own
    labels and mangling them would be worse than the risk they carry.
    """
    text = unicodedata.normalize("NFC", stem)
    text = "".join("_" if ch in ILLEGAL_CHARS or ch in CONTROL_CHARS else ch for ch in text)
    text = _TRAILING.sub("", text)
    text = _WS_RUN.sub(" ", text)
    if not text:
        text = "file"
    if text.lower() in RESERVED_NAMES:
        text = f"_{text}"
    if len(text) > max_chars:
        # Truncate on a character boundary; keep it simple and safe.
        text = text[:max_chars].rstrip(" .")
        if not text:
            text = "file"
    return text


def sanitize_filename(name: str, max_chars: int = STEM_MAX_CHARS) -> str:
    """Sanitise a full filename, lowercasing the extension."""
    stem, ext = split_ext(name)
    stem = sanitize_stem(stem, max_chars)
    if ext:
        return f"{stem}.{ext.lower()}"
    return stem


def sanitize_source_key(root: str) -> str:
    """Stable, filesystem-safe directory name for a source root.

    A drive root becomes the drive letter (``C``); anything else becomes its
    folder name.  Collisions are resolved by the caller appending ``-2``.
    """
    p = Path(root)
    try:
        p = p.resolve()
    except OSError:
        pass
    drive = p.drive.rstrip(":\\/")
    if drive and p.parent == p:          # filesystem root, e.g. C:\
        return sanitize_stem(drive or "root", 40) or "root"
    name = p.name or drive or "root"
    key = sanitize_stem(name, 40)
    return key or "root"


def human_size(n: int | float | None) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_duration(ms: int | None) -> str:
    if not ms:
        return ""
    total = int(round(ms / 1000))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


__all__ = [
    "split_ext", "media_type_for", "is_indexable", "is_junk_name", "is_hidden",
    "stat_is_hidden", "path_is_hidden", "sanitize_stem", "sanitize_filename",
    "sanitize_source_key", "human_size", "format_duration",
]
