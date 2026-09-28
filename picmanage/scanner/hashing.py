"""Content hashing: sha256 for exact identity, aHash for near-duplicates."""

from __future__ import annotations

import hashlib
import io
import logging
import os
from pathlib import Path

from ..config import HASH_CHUNK, PHASH_SIZE
from ..longpath import win_long

log = logging.getLogger(__name__)


def sha256_file(path: str | Path, *, chunk: int = HASH_CHUNK) -> str | None:
    """Streamed sha256 of a file's bytes.

    Returns ``None`` when the file cannot be read -- the caller records an
    error row rather than aborting the run.
    """
    digest = hashlib.sha256()
    try:
        with open(win_long(path), "rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        log.debug("hash failed for %s: %s", path, exc)
        return None
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# Perceptual hash (average hash)
# --------------------------------------------------------------------------

def ahash_image(source: bytes | str | Path, size: int = PHASH_SIZE) -> str | None:
    """64-bit average hash of an image, as 16 hex chars.

    EXIF orientation is applied first so the same photo stored rotated hashes
    identically.  Returns ``None`` if the bytes are not a decodable image.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - Pillow is a hard dependency
        return None

    try:
        if isinstance(source, (bytes, bytearray)):
            im = Image.open(io.BytesIO(bytes(source)))
        else:
            im = Image.open(win_long(source))
        im.load()
        im = ImageOps.exif_transpose(im)
        im = ImageOps.grayscale(im)
        im = im.resize((size, size))
    except Exception as exc:  # DecompressionBomb, truncated file, not an image
        log.debug("phash failed: %s", exc)
        return None

    # ``tobytes`` rather than ``getdata``: same bytes, no deprecation warning
    # on Pillow >= 12 and available on every version we support.
    pixels = im.tobytes()
    if not pixels:
        return None
    mean = sum(pixels) / len(pixels)
    bits = 0
    for value in pixels:
        bits = (bits << 1) | (1 if value >= mean else 0)
    return f"{bits:0{size * size // 4}x}"


def hamming(a: str, b: str) -> int:
    """Bit distance between two equal-length hex hashes."""
    if len(a) != len(b):
        return 64
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except ValueError:
        return 64


def phash_clusters(
    hashes: dict[int, str],
    threshold: int,
) -> list[list[int]]:
    """Group ids whose hashes are within ``threshold`` bits of each other.

    Single-linkage clustering, with candidates found by *banding* rather than by
    an all-pairs scan.  The hash is split into ``bands`` equal slices and only ids
    sharing at least one slice are ever compared.  Two hashes within ``threshold``
    bits differ in at most ``threshold`` slices, so whenever ``bands >
    threshold`` at least one slice matches and no qualifying pair can be missed:
    the clustering is identical to comparing every pair, but a 50 000-image batch
    performs a few million comparisons instead of 1.25 billion.

    Ids are returned in ascending order so downstream behaviour is deterministic.
    """
    ids = sorted(hashes)
    if len(ids) < 2:
        return []

    bits = len(hashes[ids[0]]) * 4
    # All-pairs when the hashes are not one uniform width, when the width does
    # not divide evenly, or when the threshold is wide enough that banding can no
    # longer guarantee a shared slice (bands is capped by the hash width).
    bands = 8
    while bands <= threshold:
        bands *= 2
    # Decode once: the inner loop compares integers, not hex strings.
    values: dict[int, int] = {}
    for i in ids:
        if len(hashes[i]) * 4 != bits:
            values = {}
            break
        try:
            values[i] = int(hashes[i], 16)
        except ValueError:
            values = {}
            break
    banding = bool(values) and bits > 0 and bits % bands == 0

    parent = {i: i for i in ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    if banding:
        width = bits // bands
        mask = (1 << width) - 1
        buckets: dict[tuple[int, int], list[int]] = {}
        for i in ids:
            value = values[i]
            for band in range(bands):
                shift = bits - (band + 1) * width
                key = (band, (value >> shift) & mask)
                bucket = buckets.get(key)
                if bucket is None:
                    buckets[key] = [i]
                else:
                    bucket.append(i)
        for members in buckets.values():
            for pos, left in enumerate(members):
                left_val = values[left]
                left_root = find(left)
                for right in members[pos + 1:]:
                    if find(right) == left_root:
                        continue
                    if (left_val ^ values[right]).bit_count() <= threshold:
                        union(left, right)
                        left_root = find(left)
    else:
        for pos, left in enumerate(ids):
            for right in ids[pos + 1:]:
                if find(left) == find(right):
                    continue
                if hamming(hashes[left], hashes[right]) <= threshold:
                    union(left, right)

    groups: dict[int, list[int]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    return [groups[k] for k in sorted(groups) if len(groups[k]) > 1]


def quick_hash_key(path: str | Path, size: int, mtime: float) -> str:
    """Cache key for files whose content hash is unavailable."""
    raw = f"{os.path.abspath(str(path))}|{size}|{mtime:.3f}".encode("utf-8", "replace")
    return hashlib.md5(raw).hexdigest()


__all__ = [
    "sha256_file", "sha256_bytes", "ahash_image", "hamming",
    "phash_clusters", "quick_hash_key",
]
