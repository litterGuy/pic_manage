"""Long-path (``\\\\?\\``) helpers for Windows.

NTFS accepts paths up to 32767 characters once the ``\\\\?\\`` prefix is used,
but the Win32 layer caps most APIs at ``MAX_PATH`` (260) unless the process
opts into long paths.  The prefix disables path normalisation, so callers must
hand us an already-normalised absolute path.

The prefix is applied **only** to source-side reads and copies.  Library paths
are short by construction and must never be handed to ``send_file``.
"""

from __future__ import annotations

import os
from pathlib import Path

IS_WINDOWS = os.name == "nt"

#: Applied when a normalised absolute path reaches this many characters.
THRESHOLD = 240

_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"


def win_long(path: str | os.PathLike[str]) -> str:
    """Return ``path`` with the ``\\\\?\\`` prefix when it is long enough.

    Idempotent: an already-prefixed path is returned unchanged.
    """
    p = os.fspath(path)
    if not IS_WINDOWS:
        return p
    if p.startswith(_PREFIX) or p.startswith("\\\\.\\"):
        return p
    if len(p) < THRESHOLD:
        return p
    # normpath collapses "." / ".." which \\?\ refuses to resolve.
    p = os.path.normpath(p)
    if p.startswith("\\\\"):
        # UNC: \\server\share\... -> \\?\UNC\server\share\...
        return _UNC_PREFIX + p[2:].lstrip("\\")
    return _PREFIX + p


def win_unlong(path: str) -> str:
    """Strip the ``\\\\?\\`` prefix, recovering a displayable path."""
    if path.startswith(_UNC_PREFIX):
        return "\\\\" + path[len(_UNC_PREFIX):]
    if path.startswith(_PREFIX):
        return path[len(_PREFIX):]
    return path


def is_long_path(path: str | os.PathLike[str]) -> bool:
    return IS_WINDOWS and len(os.fspath(path)) >= THRESHOLD


def path_length(path: str | os.PathLike[str]) -> int:
    """Length as the Win32 layer would see it (prefix not counted)."""
    return len(win_unlong(os.fspath(path)))


def same_file(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    """Best-effort identity check that tolerates long-path prefixes."""
    try:
        sa, sb = os.stat(win_long(a)), os.stat(win_long(b))
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def is_relative_to(path: Path, parent: Path) -> bool:
    """``Path.is_relative_to`` backport that is case-insensitive on Windows."""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        pass
    if not IS_WINDOWS:
        return False
    try:
        p = os.path.normcase(str(path.resolve()))
        q = os.path.normcase(str(parent.resolve()))
    except OSError:
        return False
    return p == q or p.startswith(q.rstrip("\\/") + "\\")


__all__ = [
    "IS_WINDOWS", "THRESHOLD", "win_long", "win_unlong", "is_long_path",
    "path_length", "same_file", "is_relative_to",
]
