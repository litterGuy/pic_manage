"""Command-line interface: ``python run.py <command>``.

Every subcommand is safe to re-run.  Nothing deletes user data unless the
operator passes an explicit ``--yes`` alongside a destructive flag.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .config import Settings, default_data_dir, resolve_settings
from .db import get_file, open_db, recent_runs
from .scanner.actions import sweep_partials
from .scanner.classify import human_size
from .scanner.pipeline import Progress, ScanOptions, run_scan
from .thumbs import cache_size, gc as thumbs_gc, warm

log = logging.getLogger("picmanage")


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pic_manage",
        description="扫描、去重、按拍摄时间归档媒体文件，并提供网页浏览。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python run.py scan --source D:\\照片\n"
            "  python run.py scan --source D:\\照片 --dry-run\n"
            "  python run.py scan --source D:\\照片 --move --yes\n"
            "  python run.py serve --port 8000\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"pic_manage {__version__}")
    parser.add_argument("--data-dir", default=None,
                        help=f"数据目录（默认 {default_data_dir()}，或用 PICMANAGE_HOME）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出错误")

    sub = parser.add_subparsers(dest="command", required=True)

    # ---- scan ----------------------------------------------------------
    scan = sub.add_parser("scan", help="扫描源目录并归档媒体文件")
    scan.add_argument("--source", "-s", action="append", required=True, metavar="DIR",
                      help="要扫描的目录，可重复指定多个")
    scan.add_argument("--move", action="store_true",
                      help="整理后删除源文件（需配合 --yes）")
    scan.add_argument("--yes", "-y", action="store_true", help="确认执行移动操作")
    scan.add_argument("--dry-run", "-n", action="store_true",
                      help="只打印计划，不写入任何文件")
    scan.add_argument("--phash", action="store_true",
                      help="启用图片感知哈希，识别重新编码的近似重复")
    scan.add_argument("--phash-threshold", type=int, default=6,
                      help="感知哈希判定阈值（比特距离，默认 6）")
    scan.add_argument("--mtime-fallback", action="store_true",
                      help="没有内嵌时间时退回使用文件修改时间（会在界面标注）")
    scan.add_argument("--min-precision", choices=("year", "month", "day"), default="year",
                      help="归档所需的最低时间精度（默认 year）")
    scan.add_argument("--types", default="image,video,audio",
                      help="只处理这些类型，逗号分隔（默认 image,video,audio）")
    scan.add_argument("--jobs", "-j", type=int, default=8, help="并发数（默认 8）")
    scan.add_argument("--min-size", type=int, default=1, help="最小字节数（默认 1）")
    scan.add_argument("--max-size", type=int, default=None, help="最大字节数")
    scan.add_argument("--include-hidden", action="store_true", help="包含隐藏文件和目录")
    scan.add_argument("--follow-symlinks", action="store_true", help="跟随文件符号链接")
    scan.add_argument("--skip-hash-singletons", action="store_true",
                      help="跳过唯一大小文件的哈希计算（更快，但失去跨目录精确去重）")

    # ---- status --------------------------------------------------------
    sub.add_parser("status", help="显示索引统计信息").add_argument(
        "--json", action="store_true", help="以 JSON 输出")

    # ---- serve ---------------------------------------------------------
    serve = sub.add_parser("serve", help="启动网页浏览界面")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", "-p", type=int, default=8000)
    serve.add_argument("--debug", action="store_true")
    serve.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")

    # ---- thumbs --------------------------------------------------------
    thumbs = sub.add_parser("thumbs", help="生成或清理缩略图缓存")
    thumbs.add_argument("--limit", type=int, default=None, help="最多生成多少个")
    thumbs.add_argument("--jobs", "-j", type=int, default=4)
    thumbs.add_argument("--gc", action="store_true", help="清理没有被引用的缓存文件")

    # ---- quarantine ----------------------------------------------------
    quar = sub.add_parser("quarantine", help="查看或处理隔离区中的重复文件")
    quar_sub = quar.add_subparsers(dest="quarantine_command")
    quar_sub.add_parser("list", help="列出隔离区文件")
    q_restore = quar_sub.add_parser("restore", help="把隔离区文件移回原位置")
    q_restore.add_argument("ids", nargs="+", type=int)
    q_purge = quar_sub.add_parser("purge", help="永久删除隔离区文件")
    q_purge.add_argument("ids", nargs="+", type=int)
    q_purge.add_argument("--yes", "-y", action="store_true", required=True,
                         help="确认永久删除")

    # ---- unsorted ------------------------------------------------------
    unsorted = sub.add_parser("unsorted", help="列出没有内嵌时间的文件")
    unsorted.add_argument("action", nargs="?", default="list",
                          choices=("list", "ls"), help="默认 list")

    # ---- verify --------------------------------------------------------
    verify = sub.add_parser("verify", help="校验库中文件是否完好")
    verify.add_argument("--fix", action="store_true", help="把不存在的文件标记为错误")

    # ---- db ------------------------------------------------------------
    dbp = sub.add_parser("db", help="数据库维护")
    db_sub = dbp.add_subparsers(dest="db_command")
    db_sub.add_parser("path", help="打印数据库路径")
    db_sub.add_parser("init", help="创建数据库和目录结构")
    db_sub.add_parser("vacuum", help="压缩数据库")

    # ---- clean-partials ------------------------------------------------
    sub.add_parser("clean-partials", help="清理中断运行残留的 .part 临时文件")

    return parser


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def _setup_logging(args) -> None:
    level = logging.WARNING
    if args.verbose:
        level = logging.DEBUG
    elif args.quiet:
        level = logging.ERROR
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def _settings_from(args, **kwargs) -> Settings:
    return resolve_settings(getattr(args, "data_dir", None), **kwargs)


def _open_existing(args, *, hint: str):
    """Open the index, or explain how to create it instead of raising.

    ``connect(create=False)`` raises ``FileNotFoundError`` on a fresh install,
    which used to surface as a raw traceback from every read-only subcommand.
    """
    settings = _settings_from(args)
    if not settings.db_path.exists():
        print(f"还没有索引。先运行: {hint}")
        return None, None
    return settings, open_db(settings.db_path, create=False)


def _parse_types(text: str) -> tuple[str, ...]:
    valid = {"image", "video", "audio"}
    picked = {t.strip().lower() for t in text.split(",") if t.strip()}
    unknown = picked - valid
    if unknown:
        raise SystemExit(f"未知的媒体类型: {', '.join(sorted(unknown))}"
                         f"（可用: image, video, audio）")
    if not picked:
        raise SystemExit("--types 不能为空")
    return tuple(sorted(picked))


def cmd_scan(args) -> int:
    settings = _settings_from(args)
    roots = [str(Path(r).expanduser()) for r in args.source]
    missing = [r for r in roots if not Path(r).exists()]
    if missing:
        for m in missing:
            print(f"错误: 目录不存在 {m}", file=sys.stderr)
        return 2

    if args.move and not args.yes:
        print("错误: --move 会删除源文件，必须同时加上 --yes 确认。", file=sys.stderr)
        return 2

    types = _parse_types(args.types)
    settings.types = types
    settings.ensure_dirs()

    conn = open_db(settings.db_path)
    try:
        opts = ScanOptions(
            source_roots=roots,
            move=args.move, assume_yes=args.yes, dry_run=args.dry_run,
            phash=args.phash, phash_threshold=args.phash_threshold,
            mtime_fallback=args.mtime_fallback, min_precision=args.min_precision,
            jobs=args.jobs, types=types, min_size=args.min_size,
            max_size=args.max_size, include_hidden=args.include_hidden,
            follow_symlinks=args.follow_symlinks,
            skip_hash_singletons=args.skip_hash_singletons,
            argv=sys.argv,
        )
        run_scan(conn, settings, opts, progress=Progress(quiet=args.quiet))
        return 0
    finally:
        conn.close()


def cmd_status(args) -> int:
    settings = _settings_from(args)
    if not settings.db_path.exists():
        print("还没有索引。先运行: python run.py scan --source <目录>")
        return 0

    conn = open_db(settings.db_path, create=False)
    try:
        data = _collect_stats(conn, settings)
    finally:
        conn.close()

    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print(f"数据目录: {settings.data_dir}")
    print(f"媒体库:   {settings.library_dir}")
    print()
    print(f"已归档   {data['organized']:>8,} 个文件  ({human_size(data['organized_bytes'])})")
    print(f"未分类   {data['unsorted']:>8,} 个文件")
    print(f"重复隔离 {data['quarantined']:>8,} 个文件  ({human_size(data['quarantined_bytes'])})")
    print(f"错误     {data['errors']:>8,} 个文件")
    print()
    print("按类型:")
    for mtype, count in sorted(data["by_type"].items()):
        print(f"  {mtype:<8} {count:>8,}")
    print()
    if data["months"]:
        newest, oldest = data["months"][0], data["months"][-1]
        print(f"时间范围: {oldest[0]}-{oldest[1]:02d}  到  {newest[0]}-{newest[1]:02d}"
              f"  （共 {len(data['months'])} 个月份）")
    print(f"缩略图缓存: {data['thumb_count']:,} 个，{human_size(data['thumb_bytes'])}")

    runs = data["runs"]
    if runs:
        print()
        print("最近的运行:")
        for r in runs[:5]:
            state = "中断" if r["interrupted"] else ("预演" if r["mode"] == "dry-run" else "完成")
            print(f"  #{r['id']:<4} {r['started_at']}  {state}  "
                  f"归档 {r['organized']} / 未分类 {r['unsorted']} / "
                  f"隔离 {r['quarantined']} / 错误 {r['errors']}")
    return 0


def _collect_stats(conn, settings: Settings) -> dict:
    def scalar(sql, params=()):
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else 0

    data = {
        "organized": scalar("SELECT COUNT(*) FROM files WHERE status='organized'"),
        "unsorted": scalar("SELECT COUNT(*) FROM files WHERE status='unsorted'"),
        "quarantined": scalar("SELECT COUNT(*) FROM files WHERE status='quarantined'"),
        "errors": scalar("SELECT COUNT(*) FROM files WHERE status='error'"),
        "organized_bytes": scalar(
            "SELECT COALESCE(SUM(size),0) FROM files WHERE status='organized'"),
        "quarantined_bytes": scalar(
            "SELECT COALESCE(SUM(size),0) FROM files WHERE status='quarantined'"),
        "by_type": {
            row[0]: row[1] for row in conn.execute(
                "SELECT media_type, COUNT(*) FROM files "
                "WHERE status='organized' GROUP BY media_type")
        },
        "months": [(row[0], row[1]) for row in conn.execute(
            "SELECT DISTINCT taken_year, taken_month FROM files "
            "WHERE status='organized' AND taken_year IS NOT NULL "
            "ORDER BY taken_year DESC, taken_month DESC")],
        "thumb_count": 0,
        "thumb_bytes": cache_size(settings),
        "runs": [],
    }
    if settings.thumbs_dir.exists():
        data["thumb_count"] = sum(
            1 for e in settings.thumbs_dir.iterdir() if e.suffix.lower() == ".jpg")
    for r in recent_runs(conn, 10):
        data["runs"].append(dict(r))
    return data


def cmd_serve(args) -> int:
    settings = _settings_from(args)
    if not settings.db_path.exists():
        print("还没有索引。先运行: python run.py scan --source <目录>")
        return 1
    settings.ensure_dirs()

    from .web import create_app

    app = create_app(settings)
    url = f"http://{args.host}:{args.port}/"
    if not args.quiet:
        print(f"媒体库浏览界面: {url}")
        print(f"数据目录: {settings.data_dir}")
        print("按 Ctrl+C 停止。")
    if not args.no_browser and not args.debug and not args.quiet:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


_SCAN_HINT = "python run.py scan --source <目录>"


def cmd_thumbs(args) -> int:
    settings, conn = _open_existing(args, hint=_SCAN_HINT)
    if conn is None:
        return 0
    try:
        if args.gc:
            removed = thumbs_gc(settings, conn)
            print(f"清理了 {removed} 个未被引用的缩略图")
            return 0
        generated, failed = warm(settings, conn, limit=args.limit, jobs=args.jobs)
        print(f"生成缩略图 {generated} 个，跳过/失败 {failed} 个")
        print(f"缓存目录: {settings.thumbs_dir}（{human_size(cache_size(settings))}）")
        return 0
    finally:
        conn.close()


def cmd_quarantine(args) -> int:
    settings, conn = _open_existing(args, hint=_SCAN_HINT)
    if conn is None:
        return 0
    try:
        sub = getattr(args, "quarantine_command", None) or "list"
        if sub == "list":
            return _quarantine_list(conn, settings)
        if sub == "restore":
            return _quarantine_restore(conn, settings, args.ids)
        if sub == "purge":
            return _quarantine_purge(conn, settings, args.ids)
        return 1
    finally:
        conn.close()


def _quarantine_list(conn, settings) -> int:
    rows = list(conn.execute(
        "SELECT * FROM files WHERE status='quarantined' ORDER BY source_path"))
    if not rows:
        print("隔离区是空的。")
        return 0
    total = sum(r["size"] for r in rows)
    print(f"隔离区共 {len(rows)} 个重复文件，合计 {human_size(total)}")
    print(f"位置: {settings.quarantine_dir}\n")
    current = None
    for r in rows:
        try:
            rel = str(Path(r["dest_path"]).relative_to(settings.quarantine_dir))
        except ValueError:
            rel = r["dest_path"] or ""
        key = rel.split("\\")[0].split("/")[0]
        if key != current:
            current = key
            print(f"[{key}]")
        name = Path(rel).name
        print(f"  #{r['id']:<6} {name:<40} {human_size(r['size']):>10}  "
              f"{r['time_source'] or '-'}  {r['taken_at'] or '-'}")
    return 0


def _quarantine_restore(conn, settings, ids: list[int]) -> int:
    from .db import log_action, update_file

    restored = 0
    for file_id in ids:
        row = get_file(conn, file_id)
        if not row or row["status"] != "quarantined":
            print(f"#{file_id}: 不是隔离区文件，跳过")
            continue
        src = row["dest_path"]
        if not src or not Path(src).exists():
            print(f"#{file_id}: 隔离区文件已不存在，跳过")
            continue
        target = row["source_path"]
        if Path(target).exists():
            print(f"#{file_id}: 原位置已有文件，跳过 ({target})")
            continue
        try:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, target)
        except OSError as exc:
            print(f"#{file_id}: 恢复失败 {exc}")
            continue
        update_file(conn, file_id, status="organized", dest_path=target)
        log_action(conn, _last_run(conn), file_id, "restore", src, target)
        restored += 1
        print(f"#{file_id}: 已恢复 -> {target}")
    print(f"共恢复 {restored} 个文件。")
    return 0


def _quarantine_purge(conn, settings, ids: list[int]) -> int:
    from .db import log_action, update_file

    purged = 0
    freed = 0
    for file_id in ids:
        row = get_file(conn, file_id)
        if not row or row["status"] != "quarantined":
            print(f"#{file_id}: 不是隔离区文件，跳过")
            continue
        path = row["dest_path"]
        try:
            if path and Path(path).exists():
                freed += Path(path).stat().st_size
                Path(path).unlink()
        except OSError as exc:
            print(f"#{file_id}: 删除失败 {exc}")
            continue
        update_file(conn, file_id, status="skipped", dest_path=None,
                    skip_reason="purged")
        log_action(conn, _last_run(conn), file_id, "purge", path, None)
        purged += 1
        print(f"#{file_id}: 已删除")
    print(f"共删除 {purged} 个文件，释放 {human_size(freed)}。")
    return 0


def _last_run(conn) -> int:
    row = conn.execute("SELECT MAX(id) FROM runs").fetchone()
    return int(row[0]) if row and row[0] else 0


def cmd_unsorted(args) -> int:
    settings, conn = _open_existing(args, hint=_SCAN_HINT)
    if conn is None:
        return 0
    try:
        rows = list(conn.execute(
            "SELECT * FROM files WHERE status='unsorted' ORDER BY source_path"))
        if not rows:
            print("没有未分类文件。")
            return 0
        total = sum(r["size"] for r in rows)
        print(f"共 {len(rows)} 个文件没有找到内嵌时间，合计 {human_size(total)}")
        print(f"位置: {settings.unsorted_dir}")
        print("可在网页界面的「未分类」页面手动指定年月。\n")
        for r in rows:
            print(f"  #{r['id']:<6} {Path(r['source_path']).name:<44} "
                  f"{human_size(r['size']):>10}  {r['media_type']}")
        return 0
    finally:
        conn.close()


def cmd_verify(args) -> int:
    settings, conn = _open_existing(args, hint=_SCAN_HINT)
    if conn is None:
        return 0
    try:
        from .db import update_file

        missing = mismatch = ok = 0
        for row in conn.execute(
                "SELECT id, dest_path, size, sha256 FROM files "
                "WHERE status IN ('organized','unsorted') AND dest_path IS NOT NULL"):
            path = Path(row["dest_path"])
            if not path.exists():
                missing += 1
                print(f"  缺失  #{row['id']}  {path}")
                if args.fix:
                    update_file(conn, row["id"], status="error", error="missing")
                continue
            if path.stat().st_size != row["size"]:
                mismatch += 1
                print(f"  大小不符 #{row['id']}  {path}")
                continue
            ok += 1
        print(f"完好 {ok} 个，缺失 {missing} 个，大小不符 {mismatch} 个。")
        return 0 if (missing == 0 and mismatch == 0) else 1
    finally:
        conn.close()


def cmd_db(args) -> int:
    settings = _settings_from(args)
    sub = getattr(args, "db_command", None) or "path"
    if sub == "path":
        print(settings.db_path)
    elif sub == "init":
        settings.ensure_dirs()
        conn = open_db(settings.db_path)
        conn.close()
        print(f"已初始化: {settings.data_dir}")
    elif sub == "vacuum":
        if not settings.db_path.exists():
            print("数据库不存在。")
            return 1
        conn = open_db(settings.db_path, create=False)
        try:
            before = settings.db_path.stat().st_size
            conn.execute("VACUUM")
            after = settings.db_path.stat().st_size
            print(f"压缩完成: {human_size(before)} -> {human_size(after)}")
        finally:
            conn.close()
    return 0


def cmd_clean_partials(args) -> int:
    settings = _settings_from(args)
    n = sweep_partials(settings.library_dir) + sweep_partials(settings.quarantine_dir)
    print(f"清理了 {n} 个 .part 临时文件。")
    return 0


_COMMANDS = {
    "scan": cmd_scan,
    "status": cmd_status,
    "serve": cmd_serve,
    "thumbs": cmd_thumbs,
    "quarantine": cmd_quarantine,
    "unsorted": cmd_unsorted,
    "verify": cmd_verify,
    "db": cmd_db,
    "clean-partials": cmd_clean_partials,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args)
    handler = _COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\n已中断。下次运行会从中断处继续。")
        return 130
    except BrokenPipeError:
        return 0


def console_main() -> None:
    """Entry point for the installed ``pic-manage`` script.

    ``run.py`` calls :func:`configure_stdio` itself before importing this
    module; a console script bypasses it, so the encoding fix-up has to happen
    here or Chinese output turns into mojibake on a GBK console.
    """
    from .console import configure_stdio

    configure_stdio()
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
