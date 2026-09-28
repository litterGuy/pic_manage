"""Image metadata probing: embedded creation time, dimensions, orientation.

The single most important rule in this project lives here: **the time a file
was copied into a directory is not its creation time.**  We read only what the
encoder actually wrote into the file.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..config import MAX_IMAGE_PIXELS
from ..longpath import win_long
from ..timeparse import ParsedTime, parse_datetime

log = logging.getLogger(__name__)

# EXIF tags, in the order a photographer would expect them to mean "taken at".
TAG_DATETIME_ORIGINAL = 36867
TAG_DATETIME_DIGITIZED = 36868
TAG_DATETIME = 306
TAG_ORIENTATION = 274

EXIF_DATE_TAGS = (TAG_DATETIME_ORIGINAL, TAG_DATETIME_DIGITIZED, TAG_DATETIME)

# XMP date properties, most specific first.  Both spellings occur in the wild:
# the element form ``<xmp:CreateDate>...</xmp:CreateDate>`` and, far more
# commonly, the attribute form ``xmp:CreateDate="..."`` on an rdf:Description.
XMP_DATE_PATTERNS = (
    re.compile(rb"<\s*(?:xmp|exif):DateTimeOriginal[^>]*>([^<]+)<", re.I),
    re.compile(rb"<\s*xmp:CreateDate[^>]*>([^<]+)<", re.I),
    re.compile(rb"<\s*photoshop:DateCreated[^>]*>([^<]+)<", re.I),
    re.compile(rb"<\s*xmp:DateCreated[^>]*>([^<]+)<", re.I),
    re.compile(rb"<\s*tiff:DateTime[^>]*>([^<]+)<", re.I),
    re.compile(rb"<\s*exif:DateTimeDigitized[^>]*>([^<]+)<", re.I),
    re.compile(rb"<\s*xmp:ModifyDate[^>]*>([^<]+)<", re.I),
    re.compile(rb"<\s*xmp:MetadataDate[^>]*>([^<]+)<", re.I),
)

XMP_DATE_ATTRIBUTES = (
    rb"(?:xmp|exif):DateTimeOriginal\s*=\s*[\"']([^\"']+)[\"']",
    rb"xmp:CreateDate\s*=\s*[\"']([^\"']+)[\"']",
    rb"photoshop:DateCreated\s*=\s*[\"']([^\"']+)[\"']",
    rb"xmp:DateCreated\s*=\s*[\"']([^\"']+)[\"']",
    rb"tiff:DateTime\s*=\s*[\"']([^\"']+)[\"']",
    rb"exif:DateTimeDigitized\s*=\s*[\"']([^\"']+)[\"']",
    rb"xmp:ModifyDate\s*=\s*[\"']([^\"']+)[\"']",
    rb"xmp:MetadataDate\s*=\s*[\"']([^\"']+)[\"']",
)

# Plain-text PNG keys (Photoshop, GIMP, ImageMagick, XnView all differ).
PNG_TEXT_KEYS = (
    "date:create", "date:modify", "creation time", "date created",
    "createdate", "datetimeoriginal", "date", "creationdate",
)

_heif_registered = False


def register_heif() -> bool:
    """Register the pillow-heif opener once.  Safe to call repeatedly."""
    global _heif_registered
    if _heif_registered:
        return True
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
        _heif_registered = True
    except Exception as exc:  # pragma: no cover - optional dependency
        log.debug("pillow-heif unavailable: %s", exc)
    return _heif_registered


@dataclass
class ImageInfo:
    width: int | None = None
    height: int | None = None
    thumb_ready: int = 1
    error: str | None = None


@dataclass
class ImageTime:
    parsed: ParsedTime | None = None
    source: str = "none"
    raw: str | None = None


def _first_parsed(values) -> tuple[ParsedTime | None, str | None]:
    for value in values:
        if value is None:
            continue
        parsed = parse_datetime(value)
        if parsed is not None:
            return parsed, str(value)[:255]
    return None, None


def _from_exif(exif) -> ImageTime:
    """Read a Pillow Exif object.

    ``getexif()`` already merges the ExifIFD (0x8769) into the top-level
    mapping, so ``DateTimeOriginal`` is visible whether the camera wrote it to
    IFD0 or the sub-IFD.  ``get_ifd`` is consulted as a defensive fallback.
    """
    if exif is None:
        return ImageTime()
    try:
        merged = dict(exif)
        for ifd in (0x8769, 0x8825):  # ExifIFD, GPSInfo is irrelevant but cheap
            try:
                merged.update(dict(exif.get_ifd(ifd)))
            except Exception:
                pass
    except Exception:
        return ImageTime()

    for tag in EXIF_DATE_TAGS:
        if tag in merged:
            parsed, raw = _first_parsed([merged[tag]])
            if parsed is not None:
                return ImageTime(parsed, "exif", raw)
    return ImageTime()


def _from_xmp(blob) -> ImageTime:
    if not blob:
        return ImageTime()
    if isinstance(blob, str):
        blob = blob.encode("utf-8", "replace")
    # Element form first (rarer), then the attribute form.
    for pattern in XMP_DATE_PATTERNS:
        m = pattern.search(blob)
        if m:
            parsed, raw = _first_parsed([m.group(1)])
            if parsed is not None:
                return ImageTime(parsed, "xmp", raw)
    for pattern in XMP_DATE_ATTRIBUTES:
        m = re.search(pattern, blob, re.I)
        if m:
            parsed, raw = _first_parsed([m.group(1)])
            if parsed is not None:
                return ImageTime(parsed, "xmp", raw)
    return ImageTime()


def _from_png_text(info: dict) -> ImageTime:
    lowered = {str(k).lower(): v for k, v in info.items()}
    for key in PNG_TEXT_KEYS:
        if key in lowered:
            parsed, raw = _first_parsed([lowered[key]])
            if parsed is not None:
                return ImageTime(parsed, "xmp", raw)
    return ImageTime()


def probe_image_time(path: str | Path) -> ImageTime:
    """Extract the embedded creation time from an image file."""
    register_heif()
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
        # win_long: without it a source path past MAX_PATH raises here and the
        # file is silently filed as having no embedded time.
        with Image.open(win_long(path)) as im:
            info = dict(im.info or {})
            exif_obj = None
            try:
                exif_obj = im.getexif()
            except Exception:
                exif_obj = None
    except Exception as exc:
        log.debug("cannot open image %s: %s", path, exc)
        return ImageTime()

    # 1. EXIF (JPEG / TIFF / WebP / AVIF / HEIC / PNG eXIf)
    found = _from_exif(exif_obj)
    if found.parsed is not None:
        return found

    # 2. XMP packet
    for key in ("xmp", "XML:com.adobe.xmp", "xmp_packet", "applied_xmp"):
        if key in info:
            found = _from_xmp(info[key])
            if found.parsed is not None:
                return found

    # 3. PNG plain text chunks
    found = _from_png_text(info)
    if found.parsed is not None:
        return found

    # 4. Some writers stash raw EXIF bytes in info without a parsed object.
    raw_exif = info.get("exif")
    if raw_exif and exif_obj is None:
        try:
            from PIL import Image

            exif_obj = Image.Exif()
            exif_obj.load(raw_exif if isinstance(raw_exif, bytes) else bytes(raw_exif))
            found = _from_exif(exif_obj)
            if found.parsed is not None:
                return found
        except Exception:
            pass

    return ImageTime()


def probe_image_info(path: str | Path) -> ImageInfo:
    """Dimensions and whether a thumbnail can be produced."""
    register_heif()
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
        with Image.open(win_long(path)) as im:
            width, height = im.size
            try:
                im.load()
            except Exception as exc:
                return ImageInfo(width, height, 0, f"decode: {exc}")
            return ImageInfo(width, height, 1)
    except Exception as exc:
        # RAW formats Pillow cannot open land here: no dimensions, no thumb.
        return ImageInfo(None, None, 0, f"unreadable: {exc}"[:255])


__all__ = [
    "ImageInfo", "ImageTime", "probe_image_time", "probe_image_info",
    "register_heif", "EXIF_DATE_TAGS",
]
