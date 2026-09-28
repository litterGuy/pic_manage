"""Destination planning: where every file goes, and what it is called.

Pure logic -- it reads the filesystem and the index but never writes.  Keeping
it side-effect-free is what makes ``--dry-run`` trustworthy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..longpath import win_long
from .classify import sanitize_filename, sanitize_source_key


@dataclass
class Plan:
    """The decision for one candidate file."""

    action: str                 # 'organize' | 'unsorted' | 'quarantine' | 'skip'
    dest_path: str | None       # absolute path to write
    taken_year: int | None = None
    taken_month: int | None = None
    reason: str | None = None
    skip_reason: str | None = None


@dataclass
class _DirIndex:
    """Lowercased filenames already present in a directory.

    Names are folded to lower case on the way in because Windows folders
    cannot hold two entries differing only by case.
    """

    names: set[str]

    def __post_init__(self) -> None:
        self.names = {n.lower() for n in self.names}

    def free(self, name: str) -> str | None:
        """First free variant of ``name``, or ``None`` if it is already there.

        ``name`` itself returns ``None`` when taken -- the caller then inspects
        the existing row to decide whether this is an idempotent re-run or a
        genuine collision.
        """
        low = name.lower()
        if low not in self.names:
            return name
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
            suffix_base = name
        else:
            suffix_base = stem
        n = 2
        while True:
            candidate = f"{suffix_base}-{n}.{ext}" if ext else f"{suffix_base}-{n}"
            if candidate.lower() not in self.names:
                return candidate
            n += 1
            if n > 9999:
                return None

    def add(self, name: str) -> None:
        self.names.add(name.lower())


def _scan_dir(path: Path) -> _DirIndex:
    try:
        with os.scandir(win_long(path)) as it:
            return _DirIndex({e.name.lower() for e in it})
    except OSError:
        return _DirIndex(set())


def unique_dest(dest_dir: Path, filename: str, index: _DirIndex) -> str | None:
    """Resolve a destination filename, appending ``-2``/``-3`` on collision."""
    chosen = index.free(filename)
    if chosen is None:
        return None
    index.add(chosen)
    return str(dest_dir / chosen)


def quarantine_path(
    quarantine_root: Path,
    source_key: str,
    relpath: str,
    index: _DirIndex | None = None,
) -> str | None:
    """Preserve the source's relative structure under the quarantine root.

    Two files with the same name from different source roots cannot collide
    because ``source_key`` separates them; two from the same root keep their
    relative paths for the same reason.  ``-2`` is only appended when a
    genuinely different file already occupies the slot.
    """
    parts = [sanitize_filename(p) for p in Path(relpath).parts if p not in (".", "..")]
    if not parts:
        parts = ["file"]
    target_dir = quarantine_root.joinpath(source_key, *parts[:-1])
    name = parts[-1]

    if index is None or index.names is None:
        index = _scan_dir(target_dir)

    existing = None
    for candidate_dir in (target_dir,):
        try:
            for entry in os.scandir(win_long(candidate_dir)):
                if entry.name.lower() == name.lower():
                    existing = entry.name
                    break
        except OSError:
            pass

    if existing is None:
        chosen = name
    else:
        # Already present.  Caller compares hashes; we only need a free slot.
        stem, dot, ext = name.rpartition(".")
        if dot:
            base, e = stem, ext
        else:
            base, e = name, ""
        n = 2
        while True:
            chosen = f"{base}-{n}.{e}" if e else f"{base}-{n}"
            try:
                taken = any(
                    entry.name.lower() == chosen.lower()
                    for entry in os.scandir(win_long(target_dir))
                )
            except OSError:
                taken = False
            if not taken:
                break
            n += 1
            if n > 9999:
                return None
    index.add(chosen)
    return str(target_dir / chosen)


def plan_organize(
    library_dir: Path,
    filename: str,
    rel_dir: str | None,
) -> tuple[str, _DirIndex | None]:
    """Target path inside ``library/<YYYY>/<YYYY-MM>/`` for a known date."""
    target_dir = library_dir.joinpath(*rel_dir.split("/")) if rel_dir else library_dir
    return str(target_dir), _scan_dir(target_dir)


def plan_unsorted(unsorted_dir: Path, filename: str, index: _DirIndex) -> str | None:
    """Target path inside ``library/_unsorted/``."""
    chosen = index.free(filename)
    if chosen is None:
        return None
    index.add(chosen)
    return str(unsorted_dir / chosen)


__all__ = [
    "Plan", "unique_dest", "quarantine_path", "plan_organize",
    "plan_unsorted", "sanitize_source_key",
]
