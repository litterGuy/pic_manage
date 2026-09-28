"""Paths, extension tables and tunables for pic_manage.

Everything runtime-related is derived from the data root so that a single
``--data-dir`` / ``PICMANAGE_HOME`` override relocates all state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# Media classification tables
# --------------------------------------------------------------------------

IMAGE_EXTS = {
    "jpg", "jpeg", "jpe", "jfif", "png", "webp", "avif", "gif", "bmp",
    "tif", "tiff", "heic", "heif", "hif", "dng", "cr2", "cr3", "crw",
    "nef", "nrw", "arw", "srf", "sr2", "orf", "rw2", "raf", "pef", "ptx",
    "x3f", "srw", "mrw", "mdc", "erf", "kdc", "dcr", "3fr", "fff", "iiq",
    "mos", "raw",
}

VIDEO_EXTS = {
    "mp4", "m4v", "mov", "qt", "mkv", "mk3d", "webm", "avi", "avs",
    "wmv", "asf", "flv", "f4v", "3gp", "3g2", "3gpp", "mpg", "mpeg",
    "mpe", "mpv", "m2v", "m2ts", "mts", "mxf", "ts", "tp", "vob", "rm",
    "rmvb", "divx", "ogv", "dvr-ms", "amv", "mpg2", "m1v",
}

AUDIO_EXTS = {
    "mp3", "mp2", "m4a", "m4b", "m4r", "m4p", "aax", "aac", "flac",
    "wav", "wave", "aif", "aiff", "aifc", "ogg", "oga", "opus", "spx",
    "wma", "asx", "ape", "apl", "mac", "wv", "tta", "mpc", "mp+",
    "dff", "dsf", "dsd", "amr", "ac3", "dts", "cda", "mid", "midi",
    "rmi", "kar", "ra", "au", "snd", "gsm", "ape2",
}

EXT_TO_TYPE: dict[str, str] = {}
for _ext in IMAGE_EXTS:
    EXT_TO_TYPE[_ext] = "image"
for _ext in VIDEO_EXTS:
    EXT_TO_TYPE[_ext] = "video"
for _ext in AUDIO_EXTS:
    EXT_TO_TYPE[_ext] = "audio"

#: Extensions whose bytes we are willing to hash at all.  Everything else in
#: a scan is still indexed as ``media_type='other'`` for the record but is
#: never copied into the library.
INDEXABLE_EXTS = set(EXT_TO_TYPE)

# --------------------------------------------------------------------------
# Junk / noise blocklist (matched case-insensitively)
# --------------------------------------------------------------------------

JUNK_NAMES = {
    "thumbs.db", "ehthumbs.db", "ehthumbs_vista.db", "imagestore.db",
    "desktop.ini", "folder.ini", ".ds_store", "._.ds_store", ".localized",
    ".appledouble", ".ledirectory", ".thumbnails", ".trashes", ".fseventsd",
    ".spotlight-v100", ".temporaryitems", ".volumeicon.icns", "picasa.ini",
    ".picasa.ini", ".picasaoriginals", "zthumb.db", "pspbrwse.jbf",
}

JUNK_SUFFIXES = ("~$",)          # Office lock files
JUNK_GLOBS = ("*.tmp", "*.temp", "*.part", "*.crdownload", "*.download")

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

HASH_CHUNK = 1 << 20             # 1 MiB streaming reads
DEFAULT_JOBS = 8
FFPROBE_TIMEOUT = 30             # seconds
FFMPEG_TIMEOUT = 60
BATCH_SIZE = 200                 # files per SQLite transaction
THUMB_MAX_EDGE = 360
THUMB_QUALITY = 82
PHASH_SIZE = 8
PHASH_DEFAULT_THRESHOLD = 6
MAX_IMAGE_PIXELS = 200_000_000
LONGPATH_THRESHOLD = 240         # \\?\ kicks in above this many characters
STEM_MAX_CHARS = 80
RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
ILLEGAL_CHARS = '<>:"/\\|?*'
CONTROL_CHARS = "".join(chr(c) for c in range(0x20))

UNSORTED_DIRNAME = "_unsorted"
QUARANTINE_DIRNAME = "quarantine"
THUMBS_DIRNAME = "thumbs"
LOGS_DIRNAME = "logs"
LIBRARY_DIRNAME = "library"
CACHE_DIRNAME = "cache"
DB_FILENAME = "index.db"

TIME_SOURCE_LABELS = {
    "exif": "EXIF",
    "xmp": "XMP",
    "quicktime": "QuickTime",
    "quicktime_apple": "QuickTime",
    "ffprobe_stream": "视频流",
    "ffprobe_meta": "容器元数据",
    "id3": "ID3",
    "dsf_id3": "DSF-ID3",
    "m4a": "MP4 标签",
    "flac": "Vorbis/FLAC",
    "asf": "ASF",
    "apev2": "APEv2",
    "riff_icrd": "RIFF ICRD",
    "mutagen_meta": "音频元数据",
    "manual": "手动指定",
    "mtime": "文件修改时间",
    "none": "无内嵌时间",
}

PRECISION_LABELS = {
    "second": "精确到秒",
    "minute": "精确到分钟",
    "day": "精确到天",
    "month": "仅精确到月",
    "year": "仅精确到年",
    "none": "未知",
}

MEDIA_TYPE_LABELS = {"image": "图片", "video": "视频", "audio": "音频", "other": "其他"}

STATUS_LABELS = {
    "organized": "已归档",
    "unsorted": "未分类",
    "quarantined": "重复（隔离）",
    "skipped": "已跳过",
    "error": "错误",
}


@dataclass
class Settings:
    """Resolved runtime configuration."""

    data_dir: Path
    source_roots: list[Path] = field(default_factory=list)

    # scan behaviour
    jobs: int = DEFAULT_JOBS
    move: bool = False
    assume_yes: bool = False
    dry_run: bool = False
    phash: bool = False
    phash_threshold: int = PHASH_DEFAULT_THRESHOLD
    mtime_fallback: bool = False
    min_precision: str = "year"
    types: tuple[str, ...] = ("image", "video", "audio")
    min_size: int = 1
    max_size: int | None = None
    include_hidden: bool = False
    follow_symlinks: bool = False
    skip_hash_singletons: bool = False

    # web
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False
    open_browser: bool = True

    # ---- derived paths -------------------------------------------------
    @property
    def library_dir(self) -> Path:
        return self.data_dir / LIBRARY_DIRNAME

    @property
    def unsorted_dir(self) -> Path:
        return self.data_dir / LIBRARY_DIRNAME / UNSORTED_DIRNAME

    @property
    def quarantine_dir(self) -> Path:
        return self.data_dir / QUARANTINE_DIRNAME

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / CACHE_DIRNAME / THUMBS_DIRNAME

    @property
    def log_dir(self) -> Path:
        return self.data_dir / LOGS_DIRNAME

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILENAME

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.library_dir,
            self.unsorted_dir,
            self.quarantine_dir,
            self.thumbs_dir,
            self.log_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def default_data_dir() -> Path:
    """``PICMANAGE_HOME`` wins, otherwise ``<project>/data``."""
    env = os.environ.get("PICMANAGE_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return PROJECT_ROOT / "data"


def resolve_settings(data_dir: str | None = None, **kwargs) -> Settings:
    """Build a :class:`Settings`, honouring ``PICMANAGE_HOME``/``--data-dir``."""
    root = Path(data_dir).expanduser().resolve() if data_dir else default_data_dir()
    return Settings(data_dir=root, **kwargs)
