"""Directory walking with the filters a media scan needs.

``os.scandir`` is used rather than ``Path.rglob`` because it exposes
``DirEntry.stat()`` without an extra syscall on Windows, which matters when
walking tens of thousands of files.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from ..config import Settings
from ..longpath import is_relative_to, win_long
from .classify import (
    is_hidden,
    is_indexable,
    is_junk_name,
    media_type_for,
    path_is_hidden,
    stat_is_hidden,
)

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    """A file that survived filtering and is worth hashing and probing."""

    path: str
    size: int
    mtime: float
    media_type: str = "other"
    ext: str = ""
    relpath: str = ""

    def as_dict(self) -> dict:
        return {
            "source_path": self.path,
            "size": self.size,
            "mtime": self.mtime,
            "media_type": self.media_type,
            "ext": self.ext,
        }


@dataclass
class WalkStats:
    scanned_dirs: int = 0
    skipped_dirs: int = 0
    skipped_hidden: int = 0
    skipped_junk: int = 0
    skipped_size: int = 0
    skipped_outside_types: int = 0
    errors: list[str] = field(default_factory=list)


def _entry_stat(entry: os.DirEntry, *, follow_symlinks: bool = False
                ) -> os.stat_result | None:
    try:
        return entry.stat(follow_symlinks=follow_symlinks)
    except OSError:
        return None


def iter_files(
    roots: Iterable[str | Path],
    settings: Settings,
    *,
    stats: WalkStats | None = None,
) -> Iterator[Candidate]:
    """Yield indexable media files under ``roots``.

    Excluded, in order: anything inside the data root (so scanning a parent of
    the project can never re-ingest the library), hidden entries unless
    ``include_hidden``, junk names, wrong media type, size outside the window,
    and zero-byte files.
    """
    st = stats or WalkStats()
    data_root = settings.data_dir
    allowed = set(settings.types)

    for root in roots:
        root_path = Path(root)
        try:
            root_path = root_path.resolve()
        except OSError as exc:
            log.warning("cannot resolve source root %s: %s", root, exc)
            st.errors.append(f"resolve {root}: {exc}")
            continue
        if not root_path.exists():
            log.warning("source root does not exist: %s", root_path)
            st.errors.append(f"missing {root_path}")
            continue
        if is_relative_to(root_path, data_root):
            log.warning("skipping source root inside data dir: %s", root_path)
            continue

        if root_path.is_file():
            yield from _maybe_candidate_file(root_path, settings, allowed, st)
            continue

        yield from _walk_dir(root_path, root_path, settings, allowed, st, data_root)


def _walk_dir(
    start: Path,
    root: Path,
    settings: Settings,
    allowed: set[str],
    st: WalkStats,
    data_root: Path,
) -> Iterator[Candidate]:
    # Explicit stack instead of recursion: deep trees cannot blow the limit.
    stack = [start]
    seen_dirs: set[str] = set()

    while stack:
        current = stack.pop()
        key = str(current).lower()
        if key in seen_dirs:
            continue
        seen_dirs.add(key)

        try:
            with os.scandir(win_long(current)) as it:
                entries = list(it)
        except OSError as exc:
            st.skipped_dirs += 1
            st.errors.append(f"scandir {current}: {exc}")
            log.warning("cannot read directory %s: %s", current, exc)
            continue

        st.scanned_dirs += 1
        subdirs: list[tuple[Path, os.stat_result | None]] = []

        for entry in entries:
            name = entry.name
            try:
                if entry.is_symlink():
                    if not settings.follow_symlinks:
                        continue
                    # Directory symlinks are never followed: cycle risk.
                    try:
                        if entry.is_dir(follow_symlinks=True):
                            continue
                    except OSError:
                        continue
                    # A followed *file* symlink: the media is the target, not the
                    # link, so the stat has to follow it too.  Statting the link
                    # itself yields the reparse-point payload (0 bytes on
                    # Windows), which the size filter below then discards as an
                    # empty file -- making --follow-symlinks a silent no-op.
                    follow = True
                else:
                    follow = False
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append((Path(entry.path), _entry_stat(entry)))
                    continue
            except OSError:
                continue

            if not settings.include_hidden and is_hidden(name):
                st.skipped_hidden += 1
                continue
            if is_junk_name(name):
                st.skipped_junk += 1
                continue
            if not is_indexable(name):
                st.skipped_outside_types += 1
                continue

            mtype = media_type_for(name)
            if mtype not in allowed:
                st.skipped_outside_types += 1
                continue

            stat = _entry_stat(entry, follow_symlinks=follow)
            if stat is None:
                st.errors.append(f"stat {entry.path}")
                continue

            # A file can be hidden by *attribute* with no leading dot in its
            # name, which is how Windows marks them.  Checked here because the
            # flag rides on the stat we need anyway.
            if not settings.include_hidden and stat_is_hidden(stat):
                st.skipped_hidden += 1
                continue

            size = stat.st_size
            if size <= 0:
                st.skipped_size += 1
                continue
            if size < settings.min_size:
                st.skipped_size += 1
                continue
            if settings.max_size is not None and size > settings.max_size:
                st.skipped_size += 1
                continue

            path = Path(entry.path)
            try:
                rel = path.relative_to(root)
            except ValueError:
                rel = Path(name)
            _stem, _dot, ext = name.rpartition(".")
            yield Candidate(
                path=str(path),
                size=size,
                mtime=stat.st_mtime,
                media_type=mtype,
                ext=ext.lower() if ext else "",
                relpath=str(rel),
            )

        # Depth-first so deep trees do not hold the whole listing in memory.
        for sub, sub_stat in reversed(subdirs):
            if not settings.include_hidden and (
                    path_is_hidden(sub) or stat_is_hidden(sub_stat)):
                st.skipped_hidden += 1
                continue
            if is_relative_to(sub, data_root):
                continue
            stack.append(sub)


def _maybe_candidate_file(
    path: Path,
    settings: Settings,
    allowed: set[str],
    st: WalkStats,
) -> Iterator[Candidate]:
    name = path.name
    if is_junk_name(name) or not is_indexable(name):
        return
    if not settings.include_hidden and is_hidden(name):
        st.skipped_hidden += 1
        return
    mtype = media_type_for(name)
    if mtype not in allowed:
        st.skipped_outside_types += 1
        return
    try:
        stat = path.stat()
    except OSError as exc:
        st.errors.append(f"stat {path}: {exc}")
        return
    if not settings.include_hidden and stat_is_hidden(stat):
        st.skipped_hidden += 1
        return
    if not (settings.min_size <= stat.st_size):
        st.skipped_size += 1
        return
    if settings.max_size is not None and stat.st_size > settings.max_size:
        st.skipped_size += 1
        return
    _stem, _dot, ext = name.rpartition(".")
    yield Candidate(
        path=str(path),
        size=stat.st_size,
        mtime=stat.st_mtime,
        media_type=mtype,
        ext=ext.lower() if ext else "",
        relpath=name,
    )


def group_by_size(candidates: Iterable[Candidate]) -> dict[int, list[Candidate]]:
    """Bucket candidates by size -- the cheap pre-filter for exact dedup."""
    groups: dict[int, list[Candidate]] = {}
    for cand in candidates:
        groups.setdefault(cand.size, []).append(cand)
    return groups


__all__ = ["Candidate", "WalkStats", "iter_files", "group_by_size"]
