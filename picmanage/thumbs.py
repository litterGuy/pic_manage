"""Thumbnail generation and caching.

Thumbnails are content-addressed by ``sha256[:32]``, so a file whose bytes
change automatically gets a new cache entry and stale entries are simply
unreferenced.  Writes go to a unique temp name and are then ``os.replace``-d,
which makes concurrent generation of the same key safe.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .config import (
    FFMPEG_TIMEOUT,
    THUMB_MAX_EDGE,
    THUMB_QUALITY,
    Settings,
)
from .longpath import win_long
from .scanner.hashing import quick_hash_key
from .scanner.probe_image import register_heif

log = logging.getLogger(__name__)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: In-process locks, one per cache key, so two threads never do the same work.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def cache_key(sha256: str | None, path: str, size: int, mtime: float) -> str:
    """Content-addressed cache key, falling back to path+stat identity."""
    if sha256:
        return sha256[:32]
    return quick_hash_key(path, size, mtime)


def thumb_path(settings: Settings, key: str) -> Path:
    return settings.thumbs_dir / f"{key}.jpg"


def has_thumb(settings: Settings, key: str) -> bool:
    try:
        return thumb_path(settings, key).stat().st_size > 0
    except OSError:
        return False


# --------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------

def _save_jpeg(im, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}.jpg"
    try:
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.save(tmp, format="JPEG", quality=THUMB_QUALITY, optimize=True)
        _replace(tmp, dest)
        return True
    except Exception as exc:
        log.debug("thumb save failed %s: %s", dest, exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        # Another writer may have already put a valid file there.  Because the
        # cache key is content-derived, that file is byte-identical to ours.
        return dest.exists() and dest.stat().st_size > 0


def _replace(tmp: Path, dest: Path, attempts: int = 4) -> None:
    """``os.replace`` with a retry, for Windows' shared-handle behaviour.

    ``MoveFileEx`` refuses to overwrite a file that any process currently has
    open -- an antivirus scanner or the search indexer touching a freshly
    written thumbnail is enough.  Every writer produces the same bytes, so a
    lost race is harmless and the destination simply appears a moment later.
    """
    delay = 0.01
    for i in range(attempts):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if dest.exists() and dest.stat().st_size > 0:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return
            if i == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2
        except OSError:
            if i == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2


def image_thumb(source: str | Path, dest: Path, max_edge: int = THUMB_MAX_EDGE) -> bool:
    """Resize an image to fit ``max_edge``, honouring EXIF orientation."""
    # pillow-heif does not register itself on import.  The scanner happens to
    # call ``register_heif()`` while probing, so a CLI run that scans first
    # masks this; the web server and ``thumbs`` go straight to Pillow, and
    # every HEIC/AVIF thumbnail would come back as a 503.
    register_heif()
    try:
        from PIL import Image, ImageOps

        with Image.open(win_long(source)) as im:
            im = ImageOps.exif_transpose(im)
            im.load()
            im = ImageOps.contain(im, (max_edge, max_edge))
            return _save_jpeg(im, dest)
    except Exception as exc:
        log.debug("image thumb failed for %s: %s", source, exc)
        return False


def video_thumb(source: str | Path, dest: Path, duration_ms: int | None = None,
                max_edge: int = THUMB_MAX_EDGE) -> bool:
    """Grab a frame from a video with ffmpeg.

    The first attempt seeks to a quarter of the way in (fast, keyframe-based).
    If that yields nothing -- some containers have no early keyframe -- the seek
    is retried *after* ``-i``, which is accurate but decodes from the start.
    """
    seek = 1.0
    if duration_ms and duration_ms > 0:
        seek = min(30.0, (duration_ms / 1000.0) * 0.25)
    vf = f"scale={max_edge}:{max_edge}:force_original_aspect_ratio=decrease"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}.jpg"

    for ss_before in (True, False):
        cmd = ["ffmpeg", "-v", "quiet", "-y"]
        if ss_before:
            cmd += ["-ss", f"{seek:.3f}"]
        cmd += ["-i", win_long(source)]
        if not ss_before:
            cmd += ["-ss", f"{seek:.3f}"]
        cmd += ["-frames:v", "1", "-vf", vf, "-q:v", "3", "-f", "image2", str(tmp)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=FFMPEG_TIMEOUT,
                stdin=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("ffmpeg thumb failed for %s: %s", source, exc)
            return False
        if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            _replace(tmp, dest)
            return True
    try:
        tmp.unlink()
    except OSError:
        pass
    return dest.exists() and dest.stat().st_size > 0


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def get_or_create(
    settings: Settings,
    *,
    file_id: int,
    dest_path: str | None,
    source_path: str,
    sha256: str | None,
    size: int,
    mtime: float,
    media_type: str,
    duration_ms: int | None = None,
) -> Path | None:
    """Return the cached thumbnail, generating it on a miss.

    Returns ``None`` when no thumbnail is possible (audio, RAW, unreadable).
    """
    if not dest_path or media_type not in ("image", "video"):
        return None

    key = cache_key(sha256, source_path, size, mtime)
    dest = thumb_path(settings, key)
    if has_thumb(settings, key):
        return dest

    with _lock_for(key):
        if has_thumb(settings, key):
            return dest
        if media_type == "image":
            ok = image_thumb(dest_path, dest)
        else:
            ok = video_thumb(dest_path, dest, duration_ms)
        return dest if ok else None


def warm(settings: Settings, conn, *, limit: int | None = None, jobs: int = 4) -> tuple[int, int]:
    """Pre-generate thumbnails for organized files that lack one.

    Returns ``(generated, failed)``.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    sql = ("SELECT id, dest_path, source_path, sha256, size, mtime, media_type, "
           "duration_ms FROM files WHERE status IN ('organized','unsorted') "
           "AND media_type IN ('image','video')")
    params: tuple = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)

    rows = list(conn.execute(sql, params))
    generated = failed = 0

    def work(row):
        return get_or_create(
            settings, file_id=row["id"], dest_path=row["dest_path"],
            source_path=row["source_path"], sha256=row["sha256"],
            size=row["size"], mtime=row["mtime"], media_type=row["media_type"],
            duration_ms=row["duration_ms"],
        )

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {pool.submit(work, r): r for r in rows}
        for fut in as_completed(futures):
            if fut.result() is None:
                failed += 1
            else:
                generated += 1
    return generated, failed


def gc(settings: Settings, conn) -> int:
    """Delete cached thumbnails no file references any more."""
    referenced = {
        cache_key(row["sha256"], row["source_path"], row["size"], row["mtime"])
        for row in conn.execute(
            "SELECT sha256, source_path, size, mtime FROM files "
            "WHERE status IN ('organized','unsorted')")
    }
    removed = 0
    if not settings.thumbs_dir.exists():
        return 0
    for entry in settings.thumbs_dir.iterdir():
        if entry.suffix.lower() != ".jpg":
            continue
        if entry.stem not in referenced:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def cache_size(settings: Settings) -> int:
    total = 0
    if not settings.thumbs_dir.exists():
        return 0
    for entry in settings.thumbs_dir.iterdir():
        try:
            total += entry.stat().st_size
        except OSError:
            pass
    return total


__all__ = [
    "get_or_create", "image_thumb", "video_thumb", "warm", "gc",
    "cache_key", "thumb_path", "has_thumb", "cache_size",
]
