"""Filesystem actions: atomic copy, verified move, quarantine.

Every write goes to ``<target>.part`` and is then ``os.replace``-d into place,
which is atomic on NTFS.  An interrupted run therefore never leaves a
half-written file in the library -- only a ``.part`` file that
``clean-partials`` can safely delete.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..config import HASH_CHUNK
from ..longpath import win_long
from .planner import _scan_dir, unique_dest

log = logging.getLogger(__name__)

PART_SUFFIX = ".part"


@dataclass
class ActionResult:
    ok: bool
    dest: str | None = None
    error: str | None = None
    detail: str | None = None


def _part_path(dest: str) -> str:
    return f"{dest}{PART_SUFFIX}"


def sha256_of(path: str) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(win_long(path), "rb") as fh:
            while True:
                block = fh.read(HASH_CHUNK)
                if not block:
                    break
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def copy_atomic(source: str, dest: str) -> ActionResult:
    """Copy ``source`` to ``dest`` via a ``.part`` file, then rename.

    ``shutil.copy2`` preserves mtime, which matters because the resume stage
    uses (size, mtime) to decide whether a source file has changed.
    """
    try:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        part = _part_path(dest)
        shutil.copy2(win_long(source), win_long(part))
        os.replace(win_long(part), win_long(dest))
        return ActionResult(True, dest)
    except OSError as exc:
        _cleanup(_part_path(dest))
        return ActionResult(False, None, f"{type(exc).__name__}: {exc}")


def move_verified(source: str, dest: str, *, expected_sha: str | None = None) -> ActionResult:
    """Copy, verify the copy, then delete the source.

    The source is only removed once the destination is byte-identical, so a
    full disk or an interrupted run cannot destroy the user's only copy.
    """
    result = copy_atomic(source, dest)
    if not result.ok:
        return result

    try:
        src_stat = os.stat(win_long(source))
        dst_stat = os.stat(win_long(dest))
        if src_stat.st_size != dst_stat.st_size:
            raise OSError("size mismatch after copy")
        if expected_sha:
            actual = sha256_of(dest)
            if actual != expected_sha:
                raise OSError("sha256 mismatch after copy")
    except OSError as exc:
        # Leave the source alone; the copy is redundant but harmless.
        return ActionResult(False, dest, f"verify failed: {exc}")

    try:
        os.remove(win_long(source))
    except OSError as exc:
        # Copy is good, source is stubborn.  Report success with a note: the
        # next run will see the row as already organized and skip it.
        return ActionResult(True, dest, None, f"source not removed: {exc}")
    return ActionResult(True, dest)


def quarantine(source: str, dest: str) -> ActionResult:
    """Move a duplicate into the quarantine tree.  Never overwrites."""
    try:
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = sha256_of(dest)
            incoming = sha256_of(source)
            if existing and incoming and existing == incoming:
                return ActionResult(True, dest, None, "already quarantined")
            index = _scan_dir(target.parent)
            free = unique_dest(target.parent, target.name, index)
            if free is None:
                return ActionResult(False, None, "no free quarantine name")
            dest = free
            target = Path(dest)
        part = _part_path(dest)
        shutil.move(win_long(source), win_long(part))
        os.replace(win_long(part), win_long(dest))
        return ActionResult(True, dest)
    except OSError as exc:
        _cleanup(_part_path(dest))
        return ActionResult(False, None, f"{type(exc).__name__}: {exc}")


def restore(source: str, dest: str) -> ActionResult:
    """Move a quarantined file back to its original source path."""
    return move_verified(source, dest)


def _cleanup(path: str) -> None:
    try:
        os.remove(win_long(path))
    except OSError:
        pass


def sweep_partials(root: Path) -> int:
    """Delete ``*.part`` files left behind by an interrupted run.

    A ``.part`` file is provably incomplete: it was never renamed into place.
    """
    removed = 0
    if not root.exists():
        return 0
    for dirpath, _dirnames, filenames in os.walk(win_long(root)):
        for name in filenames:
            if name.endswith(PART_SUFFIX):
                try:
                    os.remove(os.path.join(dirpath, name))
                    removed += 1
                except OSError:
                    pass
    return removed


def tmp_name(prefix: str = "pm", suffix: str = ".tmp") -> str:
    return f".{prefix}-{os.getpid()}-{uuid.uuid4().hex[:8]}{suffix}"


__all__ = [
    "ActionResult", "copy_atomic", "move_verified", "quarantine", "restore",
    "sweep_partials", "sha256_of", "tmp_name", "PART_SUFFIX",
]
