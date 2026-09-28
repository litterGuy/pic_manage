"""Scan pipeline: walk -> resume -> hash -> probe -> dedupe -> plan -> execute.

The pipeline is written so that every stage can be interrupted at a batch
boundary and resumed on the next run, and so that ``--dry-run`` reuses the
exact same planning code as a real run.

Dedup scope is deliberately **(files already in the library) ∪ (current
batch)**: a file already organized always wins, so re-scanning an overlapping
source tree is a no-op rather than a quarantine of the library's own copy.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..config import Settings
from ..db import (
    close_stale_runs, create_run, finish_run, indexed_snapshot, log_action,
    reserve_source_key, sha256_owners, transaction, update_file, upsert_file,
)
from ..timeparse import from_iso, iso
from .actions import ActionResult, copy_atomic, move_verified, quarantine, sweep_partials
from .classify import sanitize_filename, sanitize_source_key
from .dedupe import DedupeItem, resolve
from .hashing import ahash_image, sha256_file
from .planner import _scan_dir, quarantine_path, unique_dest
from .probe_audio import probe_audio_info
from .probe_image import probe_image_info
from .probe_video import probe_video_info
from .timeextract import TimeInfo, bucket_for, extract_time
from .walker import Candidate, WalkStats, group_by_size, iter_files

log = logging.getLogger(__name__)


@dataclass
class ScanOptions:
    """Everything the pipeline needs beyond the paths themselves."""

    source_roots: list[str]
    move: bool = False
    assume_yes: bool = False
    dry_run: bool = False
    phash: bool = False
    phash_threshold: int = 6
    mtime_fallback: bool = False
    min_precision: str = "year"
    jobs: int = 8
    types: tuple[str, ...] = ("image", "video", "audio")
    min_size: int = 1
    max_size: int | None = None
    include_hidden: bool = False
    follow_symlinks: bool = False
    skip_hash_singletons: bool = False
    argv: tuple[str, ...] = ()


@dataclass
class ScanReport:
    files_seen: int = 0
    files_skipped: int = 0
    files_probed: int = 0
    organized: int = 0
    unsorted: int = 0
    quarantined: int = 0
    already: int = 0
    errors: int = 0
    exact_groups: int = 0
    phash_groups: int = 0
    bytes_copied: int = 0
    duration_s: float = 0.0
    run_id: int | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "files_seen": self.files_seen, "files_skipped": self.files_skipped,
            "files_probed": self.files_probed, "organized": self.organized,
            "unsorted": self.unsorted, "quarantined": self.quarantined,
            "errors": self.errors, "exact_groups": self.exact_groups,
            "phash_groups": self.phash_groups,
        }


class Progress:
    """Single-line progress rendering that works in PowerShell."""

    #: Never repaint more often than this many milliseconds.
    MIN_INTERVAL = 0.1

    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self._last_len = 0
        self._last_paint = 0.0

    def stage(self, text: str) -> None:
        if not self.quiet:
            print(f"\n{text}", flush=True)

    def update(self, done: int, total: int, started: float) -> None:
        if self.quiet or not total:
            return
        now = time.monotonic()
        if done < total and (now - self._last_paint) < self.MIN_INTERVAL:
            return
        self._last_paint = now
        pct = done / total
        width = 30
        filled = int(width * pct)
        bar = "#" * filled + "." * (width - filled)
        elapsed = max(0.001, now - started)
        line = (f"  [{bar}] {pct * 100:5.1f}%  {done}/{total}  "
                f"{done / elapsed:6.1f}/s")
        pad = " " * max(0, self._last_len - len(line))
        print(f"\r{line}{pad}", end="", flush=True)
        self._last_len = len(line) + len(pad)

    def done(self) -> None:
        if not self.quiet and self._last_len:
            print(flush=True)
            self._last_len = 0
            self._last_paint = 0.0


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------

@dataclass
class Probed:
    candidate: Candidate
    taken_at: datetime | None
    time_source: str
    time_raw: str | None
    time_precision: str
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    container: str | None = None
    codec: str | None = None
    thumb_ready: int = 0
    sha256: str | None = None
    phash: str | None = None
    error: str | None = None

    def time_info(self) -> TimeInfo:
        return TimeInfo(self.taken_at, self.time_source, self.time_raw, self.time_precision)


def _probe_one(cand: Candidate, opts: ScanOptions, do_hash: bool, do_phash: bool) -> Probed:
    out = Probed(cand, None, "none", None, "none")

    try:
        info = extract_time(
            cand.path, cand.media_type,
            mtime=cand.mtime, mtime_fallback=opts.mtime_fallback,
        )
        out.taken_at, out.time_source = info.taken_at, info.time_source
        out.time_raw, out.time_precision = info.time_raw, info.time_precision
    except Exception as exc:
        out.error = f"time: {exc}"[:255]

    try:
        if cand.media_type == "image":
            meta = probe_image_info(cand.path)
            out.width, out.height, out.thumb_ready = meta.width, meta.height, meta.thumb_ready
            if meta.error and not out.error:
                out.error = meta.error
        elif cand.media_type == "video":
            meta = probe_video_info(cand.path)
            out.width, out.height = meta.width, meta.height
            out.duration_ms, out.container = meta.duration_ms, meta.container
            out.codec, out.thumb_ready = meta.codec, meta.thumb_ready
            if meta.error and not out.error:
                out.error = meta.error
        elif cand.media_type == "audio":
            meta = probe_audio_info(cand.path)
            out.duration_ms, out.container = meta.duration_ms, meta.container
            out.codec = meta.codec
    except Exception as exc:
        if not out.error:
            out.error = f"meta: {exc}"[:255]

    if do_hash:
        out.sha256 = sha256_file(cand.path)
        if out.sha256 is None:
            out.error = "unreadable"
    if do_phash and cand.media_type == "image":
        out.phash = ahash_image(cand.path)

    return out


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

@dataclass
class Decision:
    probed: Probed
    action: str                     # organize | unsorted | quarantine | skip
    dest: str | None = None
    note: str | None = None
    dup_of: int | None = None
    dup_kind: str | None = None


def _root_of(path: str, roots: list[str]) -> str:
    """Longest source root that contains ``path``.

    Containment is a *path* relation, not a string prefix: ``D:\\photos`` is a
    prefix of the string ``D:\\photos-old\\a.jpg`` but does not contain it, and
    attributing that file to the wrong root would put it under
    ``<quarantine>/<wrong-source>/../photos-old/a.jpg`` -- escaping the source
    key directory entirely.
    """
    best = ""
    best_len = -1
    file_path = Path(path)
    for root in roots:
        try:
            resolved = Path(root).resolve()
        except OSError:
            resolved = Path(root)
        try:
            file_path.relative_to(resolved)
        except ValueError:
            continue
        length = len(str(resolved))
        if length > best_len:
            best, best_len = str(resolved), length
    return best or str(Path(path).parent)


class _DirCache:
    """Cached ``os.scandir`` snapshot of a destination directory."""

    def __init__(self) -> None:
        self._cache: dict[str, object] = {}

    def get(self, path: Path):
        key = str(path).lower()
        if key not in self._cache:
            self._cache[key] = _scan_dir(path)
        return self._cache[key]


def _scan_settings(settings: Settings, opts: ScanOptions) -> Settings:
    """A copy of ``settings`` carrying this scan's filters.

    The walker reads its filters off ``Settings`` while the CLI and tests
    express them on ``ScanOptions``; deriving one from the other here means
    there is exactly one place where the two are reconciled.
    """
    import dataclasses

    return dataclasses.replace(
        settings,
        types=opts.types,
        min_size=opts.min_size,
        max_size=opts.max_size,
        include_hidden=opts.include_hidden,
        follow_symlinks=opts.follow_symlinks,
        jobs=opts.jobs,
    )


def run_scan(
    conn: sqlite3.Connection,
    settings: Settings,
    opts: ScanOptions,
    *,
    progress: Progress | None = None,
) -> ScanReport:
    prog = progress or Progress()
    report = ScanReport()
    started = time.monotonic()

    settings.ensure_dirs()
    scan_settings = _scan_settings(settings, opts)
    swept = sweep_partials(settings.library_dir) + sweep_partials(settings.quarantine_dir)
    if swept:
        report.notes.append(f"清理了 {swept} 个未完成的临时文件")

    interrupted = close_stale_runs(conn)
    mode = "dry-run" if opts.dry_run else ("move" if opts.move else "copy")
    run_id = create_run(conn, source_roots=opts.source_roots, argv=opts.argv,
                        mode=mode, options={
                            "move": opts.move, "phash": opts.phash,
                            "phash_threshold": opts.phash_threshold,
                            "mtime_fallback": opts.mtime_fallback,
                            "min_precision": opts.min_precision,
                            "types": list(opts.types), "jobs": opts.jobs,
                            "min_size": opts.min_size, "max_size": opts.max_size,
                            "include_hidden": opts.include_hidden,
                            "skip_hash_singletons": opts.skip_hash_singletons,
                        })
    report.run_id = run_id
    if interrupted:
        report.notes.append(f"上次运行被中断，已标记 {interrupted} 个未完成的运行记录")

    # ---- 1. walk -------------------------------------------------------
    prog.stage(f"[扫描] 源目录: {', '.join(opts.source_roots)}")
    walk_stats = WalkStats()
    candidates = list(iter_files(opts.source_roots, scan_settings, stats=walk_stats))
    report.files_seen = len(candidates)

    by_type: dict[str, int] = {}
    for cand in candidates:
        by_type[cand.media_type] = by_type.get(cand.media_type, 0) + 1
    breakdown = " / ".join(f"{v} {k}" for k, v in sorted(by_type.items())) or "无"
    prog.stage(f"[遍历] {len(candidates):,} 个候选文件 ({breakdown})，"
               f"共 {walk_stats.scanned_dirs:,} 个目录")
    if walk_stats.errors:
        report.notes.append(f"{len(walk_stats.errors)} 个目录或文件无法访问，已跳过")

    if not candidates:
        return _finish(conn, run_id, report, started, prog, "没有发现需要处理的文件")

    # ---- 2. resume -----------------------------------------------------
    known = indexed_snapshot(conn)
    todo: list[Candidate] = []
    for cand in candidates:
        row = known.get(cand.path)
        if row is not None and row[2] != "error":
            size, mtime, _status = row
            if int(size) == cand.size and abs(float(mtime) - cand.mtime) < 1e-6:
                report.files_skipped += 1
                if not opts.dry_run:
                    file_id = _row_id(conn, cand.path)
                    if file_id:
                        update_file(conn, file_id, last_seen_run=run_id,
                                    last_seen_at=_now_iso())
                continue
        todo.append(cand)

    prog.stage(f"[索引] {report.files_skipped:,} 个文件已处理且未变化（跳过），"
               f"待处理 {len(todo):,} 个")
    if not todo:
        return _finish(conn, run_id, report, started, prog, "所有文件均已处理")

    # ---- 3. hash -------------------------------------------------------
    size_groups = group_by_size(todo)
    need_hash = {
        m.path for members in size_groups.values()
        for m in members if len(members) > 1 or not opts.skip_hash_singletons
    }
    if opts.skip_hash_singletons and len(need_hash) < len(todo):
        report.notes.append(f"跳过了 {len(todo) - len(need_hash)} 个唯一大小文件的哈希计算")

    hashed: dict[str, str | None] = {}
    if need_hash:
        prog.stage(f"[哈希] {len(need_hash):,} 个文件")
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=max(1, opts.jobs)) as pool:
            futures = {pool.submit(sha256_file, c.path): c for c in todo if c.path in need_hash}
            done = 0
            for fut in as_completed(futures):
                hashed[futures[fut].path] = fut.result()
                done += 1
                prog.update(done, len(futures), t0)
            prog.done()

    # ---- 4. probe ------------------------------------------------------
    prog.stage(f"[元数据] {len(todo):,} 个文件，{opts.jobs} 个并发")
    t0 = time.monotonic()
    probed: list[Probed] = []
    with ThreadPoolExecutor(max_workers=max(1, opts.jobs)) as pool:
        futures = {
            pool.submit(_probe_one, c, opts, c.path in need_hash, opts.phash): c
            for c in todo
        }
        done = 0
        for fut in as_completed(futures):
            try:
                probed.append(fut.result())
            except Exception as exc:
                cand = futures[fut]
                probed.append(Probed(cand, None, "none", None, "none", error=str(exc)[:255]))
            done += 1
            prog.update(done, len(todo), t0)
        prog.done()
    report.files_probed = len(probed)

    # ---- 5. dedupe -----------------------------------------------------
    # Existing library rows join the pool so their content always wins.
    owners = sha256_owners(conn, [p.sha256 for p in probed if p.sha256])
    pool: list[DedupeItem] = []
    library_items: dict[str, DedupeItem] = {}
    for sha, row in owners.items():
        item = DedupeItem(
            file_id=int(row["id"]), source_path=row["dest_path"] or row["source_path"],
            size=int(row["size"]), sha256=sha, phash=row["phash"],
            taken_at=from_iso(row["taken_at"]), time_source=row["time_source"],
            media_type=row["media_type"],
            dims=(row["width"], row["height"]) if row["width"] and row["height"] else None,
            in_library=True,
        )
        pool.append(item)
        library_items[sha] = item

    for i, p in enumerate(probed):
        dims = (p.width, p.height) if (p.width and p.height) else None
        pool.append(DedupeItem(
            file_id=-(i + 1), source_path=p.candidate.path, size=p.candidate.size,
            sha256=p.sha256, phash=p.phash, taken_at=p.taken_at,
            time_source=p.time_source, media_type=p.candidate.media_type, dims=dims,
        ))

    keepers, losers = resolve(pool, phash_threshold=opts.phash_threshold, use_phash=opts.phash)
    keeper_paths = {k.source_path for k in keepers if k.file_id < 0}
    loser_map = {l.source_path: (w, kind) for l, w, kind in losers}
    # A "group" is one set of files that are duplicates of each other, so it is
    # keyed on its winner -- counting losers instead would report the *member*
    # count, which is what made three files in one cluster read as "2 groups".
    report.exact_groups = len({w.file_id for _, w, k in losers if k == "exact"})
    report.phash_groups = len({w.file_id for _, w, k in losers if k == "phash"})
    prog.stage(f"[去重] 完全重复 {report.exact_groups} 组，近似重复 {report.phash_groups} 组，"
               f"本批保留 {len(keeper_paths):,} 个")

    # ---- 6. plan -------------------------------------------------------
    dirs = _DirCache()
    source_keys: dict[str, str] = {}

    def key_for(root: str) -> str:
        if root not in source_keys:
            source_keys[root] = (reserve_source_key(conn, root) if not opts.dry_run
                                 else sanitize_source_key(root))
        return source_keys[root]

    decisions: list[Decision] = []
    for p in probed:
        cand = p.candidate
        root = _root_of(cand.path, opts.source_roots)

        if p.sha256 and p.sha256 in library_items:
            # Identical bytes are already in the library.
            owner = owners[p.sha256]
            decisions.append(Decision(
                p, "already", owner["dest_path"],
                note=f"与库中 #{owner['id']} 内容相同",
            ))
            continue

        if cand.path in loser_map:
            winner, kind = loser_map[cand.path]
            decisions.append(Decision(
                p, "quarantine", note=f"重复（{kind}）",
                dup_of=winner.file_id if winner.file_id > 0 else None,
                dup_kind=kind,
            ))
            continue

        if cand.path not in keeper_paths:
            continue

        filename = sanitize_filename(os.path.basename(cand.path))
        rel_dir, _year = bucket_for(p.time_info(), min_precision=opts.min_precision)

        if rel_dir is None:
            dest = unique_dest(settings.unsorted_dir, filename, dirs.get(settings.unsorted_dir))
            decisions.append(Decision(p, "unsorted", dest, note="无内嵌时间"))
            continue

        target_dir = settings.library_dir.joinpath(*rel_dir.split("/"))
        dest = unique_dest(target_dir, filename, dirs.get(target_dir))
        decisions.append(Decision(p, "organize", dest, note=rel_dir))

    counts = {"organize": 0, "unsorted": 0, "quarantine": 0, "already": 0}
    for d in decisions:
        counts[d.action] = counts.get(d.action, 0) + 1
    report.organized, report.unsorted = counts["organize"], counts["unsorted"]
    report.quarantined, report.already = counts["quarantine"], counts["already"]
    prog.stage(f"[计划] 归档 {counts['organize']} / 未分类 {counts['unsorted']} / "
               f"重复隔离 {counts['quarantine']} / 已在库中 {counts['already']}")

    if opts.dry_run:
        prog.stage("[预演] 未写入任何文件（--dry-run）")
        _print_plan(prog, decisions)
        return _finish(conn, run_id, report, started, prog, "预演结束")

    # ---- 7. execute ----------------------------------------------------
    total = sum(1 for d in decisions if d.action in ("organize", "unsorted", "quarantine"))
    prog.stage(f"[执行] {'移动' if opts.move else '复制'} {total} 个文件")
    t0 = time.monotonic()
    done_count = 0

    with transaction(conn):
        for d in decisions:
            cand = d.probed.candidate
            if d.action == "already":
                log_action(conn, run_id, None, "skip", cand.path, d.dest, d.note)
                continue

            if d.action == "quarantine":
                root = _root_of(cand.path, opts.source_roots)
                rel = os.path.relpath(cand.path, root)
                qdest = quarantine_path(settings.quarantine_dir, key_for(root), rel)
                if qdest is None:
                    report.errors += 1
                    _insert_error(conn, run_id, d.probed, "无法生成隔离区路径")
                    continue
                result: ActionResult = quarantine(cand.path, qdest)
                status = "quarantined"
                year = month = None
            else:
                if d.dest is None:
                    report.errors += 1
                    _insert_error(conn, run_id, d.probed, "无法生成目标路径")
                    continue
                result = (move_verified(cand.path, d.dest, expected_sha=d.probed.sha256)
                          if opts.move else copy_atomic(cand.path, d.dest))
                status = "unsorted" if d.action == "unsorted" else "organized"
                year = d.probed.taken_at.year if d.probed.taken_at else None
                month = d.probed.taken_at.month if d.probed.taken_at else None

            if not result.ok:
                report.errors += 1
                _insert_error(conn, run_id, d.probed, result.error or "操作失败")
                log_action(conn, run_id, None, "error", cand.path, d.dest, result.error)
                continue

            file_id = _insert_probed(conn, run_id, d.probed, result.dest, status,
                                     d.probed.taken_at, d.probed.time_source,
                                     year, month, d.dup_of, d.dup_kind)
            action_name = ("quarantine" if status == "quarantined"
                           else "move" if opts.move else "copy")
            log_action(conn, run_id, file_id, action_name, cand.path, result.dest,
                       result.detail or d.note)
            if status in ("organized", "unsorted"):
                report.bytes_copied += cand.size
            done_count += 1
            prog.update(done_count, total, t0)
        prog.done()

    return _finish(conn, run_id, report, started, prog,
                   f"归档 {report.organized} / 未分类 {report.unsorted} / "
                   f"隔离 {report.quarantined} / 错误 {report.errors}")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _row_id(conn: sqlite3.Connection, source_path: str) -> int | None:
    row = conn.execute("SELECT id FROM files WHERE source_path=?", (source_path,)).fetchone()
    return int(row["id"]) if row else None


def _base_row(probed: Probed, status: str, dest: str | None,
              year: int | None, month: int | None,
              dup_of: int | None, dup_kind: str | None) -> dict:
    cand = probed.candidate
    return {
        "source_path": cand.path, "dest_path": dest, "status": status,
        "media_type": cand.media_type, "ext": cand.ext,
        "size": cand.size, "mtime": cand.mtime,
        "sha256": probed.sha256, "phash": probed.phash,
        "taken_at": iso(probed.taken_at), "taken_year": year, "taken_month": month,
        "time_source": probed.time_source, "time_raw": probed.time_raw,
        "time_precision": probed.time_precision,
        "width": probed.width, "height": probed.height,
        "duration_ms": probed.duration_ms, "container": probed.container,
        "codec": probed.codec, "thumb_ready": probed.thumb_ready,
        "dup_of": dup_of, "dup_kind": dup_kind,
    }


def _insert_probed(conn, run_id, probed, dest, status, taken_at, time_source,
                   year, month, dup_of=None, dup_kind=None) -> int:
    row = _base_row(probed, status, dest, year, month, dup_of, dup_kind)
    row["taken_at"] = iso(taken_at)
    row["time_source"] = time_source
    return upsert_file(conn, run_id, row)


def _insert_error(conn, run_id, probed, message: str) -> int:
    row = _base_row(probed, "error", None,
                    probed.taken_at.year if probed.taken_at else None,
                    probed.taken_at.month if probed.taken_at else None,
                    None, None)
    row["error"] = message[:255]
    row["thumb_ready"] = 0
    return upsert_file(conn, run_id, row)


def _finish(conn, run_id, report, started, prog, summary: str) -> ScanReport:
    report.duration_s = time.monotonic() - started
    finish_run(conn, run_id, duration_s=report.duration_s, **report.as_dict())
    prog.stage(f"[完成] 运行 #{run_id}，用时 {report.duration_s:.1f} 秒 — {summary}")
    for note in report.notes:
        prog.stage(f"  备注: {note}")
    return report


_ACTION_LABELS = {"organize": "归档", "unsorted": "未分类",
                  "quarantine": "隔离", "already": "已在库中"}


def _print_plan(prog: Progress, decisions: list[Decision], limit: int = 30) -> None:
    if prog.quiet:
        return
    shown = 0
    for d in decisions:
        if shown >= limit:
            rest = len(decisions) - shown
            if rest > 0:
                print(f"  ... 其余 {rest} 项省略")
            break
        label = _ACTION_LABELS.get(d.action, d.action)
        target = os.path.basename(d.dest) if d.dest else ""
        suffix = f"  -> {target}" if target else ""
        print(f"  [{label}] {os.path.basename(d.probed.candidate.path)}"
              f"  ({d.probed.time_source}/{d.probed.time_precision}){suffix}")
        shown += 1


__all__ = ["ScanOptions", "ScanReport", "Progress", "run_scan", "Probed", "Decision"]
