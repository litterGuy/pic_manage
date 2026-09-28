"""Flask application factory and routes.

Two invariants worth knowing before editing:

* **Files are served by ``file_id``, never by a user-supplied path.**  That
  removes path traversal as a class of bug and keeps the SQLite index the
  single source of truth for what exists.
* **A connection is opened per request** on ``flask.g``.  The database is in
  WAL mode, so a scan can write while the browser reads.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from flask import (
    Flask, abort, g, redirect, render_template, request, send_file, url_for,
)
from werkzeug.exceptions import HTTPException
from werkzeug.routing import BaseConverter, ValidationError

from ..config import (
    MEDIA_TYPE_LABELS, PRECISION_LABELS, STATUS_LABELS, TIME_SOURCE_LABELS,
    Settings,
)
from ..db import open_db, transaction, update_file
from ..scanner.actions import move_verified
from ..scanner.classify import format_duration, human_size
from ..thumbs import get_or_create
from . import queries
from .icons import glyph, icon

log = logging.getLogger(__name__)

PAGE_SIZES = {"month": 120, "all": 200}


def create_app(settings: Settings) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
        static_url_path="/static",
    )
    app.config["SETTINGS"] = settings
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True
    app.url_map.converters["file_id"] = FileIdConverter

    # ---- plumbing ------------------------------------------------------
    @app.before_request
    def _open_db() -> None:
        if "db" not in g:
            g.db = open_db(settings.db_path, create=False)

    @app.teardown_request
    def _close_db(exc) -> None:
        conn = g.pop("db", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    @app.context_processor
    def _inject_helpers():
        def media_label(t: str) -> str:
            return MEDIA_TYPE_LABELS.get(t, t)

        def source_label(t: str) -> str:
            return TIME_SOURCE_LABELS.get(t, t)

        def precision_label(t: str) -> str:
            return PRECISION_LABELS.get(t, t)

        def status_label(t: str) -> str:
            return STATUS_LABELS.get(t, t)

        def size(n) -> str:
            return human_size(n)

        def dur(ms) -> str:
            return format_duration(ms)

        def name_of(path: str | None) -> str:
            return os.path.basename(path) if path else "-"

        return dict(
            media_label=media_label, source_label=source_label,
            precision_label=precision_label, status_label=status_label,
            glyph=glyph, icon=icon, size=size, dur=dur, name_of=name_of,
            media_types=("image", "video", "audio"),
        )

    # ---- routes --------------------------------------------------------
    @app.get("/")
    def index():
        newest = queries.newest_month(g.db)
        if newest:
            return redirect(url_for("month_gallery", year=newest[0], month=newest[1]))
        return redirect(url_for("status_page"))

    @app.get("/healthz")
    def healthz():
        return "ok", 200

    @app.get("/m/<int:year>/<int:month>")
    def month_gallery(year: int, month: int):
        if not (1 <= month <= 12) or not (1 <= year <= 9999):
            abort(404)
        media_type = request.args.get("type") or None
        if media_type not in (None, "image", "video", "audio"):
            media_type = None
        sort = request.args.get("sort", "taken")
        if sort not in ("taken", "name", "size", "type"):
            sort = "taken"
        order = "desc" if request.args.get("order") == "desc" else "asc"

        page = _int_arg("page", 1, lo=1, hi=MAX_PAGE)
        limit = PAGE_SIZES["month"]
        offset = (page - 1) * limit

        total = queries.month_total(g.db, year, month, media_type)
        files = queries.month_page(
            g.db, year, month, media_type=media_type, sort=sort, order=order,
            limit=limit, offset=offset,
        )
        context = _gallery_context(
            g.db, media_type=media_type, sort=sort, order=order,
            active={"kind": "month", "year": year, "month": month},
        )
        pages = max(1, (total + limit - 1) // limit)
        return render_template(
            "gallery.html",
            title=f"{year} 年 {month} 月",
            year=year, month=month,
            files=[_decorate(f) for f in files],
            total=total, page=page, pages=pages,
            has_more=page < pages,
            **context,
        )

    @app.get("/all")
    def all_files():
        query = (request.args.get("q") or "").strip() or None
        media_type = request.args.get("type") or None
        if media_type not in (None, "image", "video", "audio"):
            media_type = None
        # lo=0, not 1: an absent or bogus year must clamp to 0 and read as "no
        # filter".  Clamping to 1 instead would silently query taken_year=1.
        year = _int_arg("year", 0, lo=0, hi=MAX_YEAR) or None
        sort = request.args.get("sort", "taken")
        if sort not in ("taken", "name", "size", "type"):
            sort = "taken"
        order = "desc" if request.args.get("order") == "desc" else "asc"

        page = _int_arg("page", 1, lo=1, hi=MAX_PAGE)
        limit = PAGE_SIZES["all"]
        offset = (page - 1) * limit

        files, total = queries.all_page(
            g.db, query=query, media_type=media_type, year=year,
            sort=sort, order=order, limit=limit, offset=offset,
        )
        context = _gallery_context(
            g.db, media_type=media_type, sort=sort, order=order,
            active={"kind": "all", "query": query, "year": year},
        )
        pages = max(1, (total + limit - 1) // limit)
        return render_template(
            "all.html",
            title="全部文件" + (f" · {query}" if query else ""),
            files=[_decorate(f) for f in files],
            total=total, page=page, pages=pages,
            query=query, year=year,
            years=queries.years_with_counts(g.db),
            **context,
        )

    @app.get("/f/<file_id:file_id>")
    def viewer(file_id: int):
        row = g.db.execute(
            "SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            abort(404)
        media_type = request.args.get("type") or None
        if media_type not in (None, "image", "video", "audio"):
            media_type = None
        scope = "all" if request.args.get("source") == "all" else "month"
        if not row["taken_year"] or not row["taken_month"]:
            scope = "all"
        query = (request.args.get("q") or "").strip() or None if scope == "all" else None
        year = _int_arg("year", 0, lo=0, hi=MAX_YEAR) or None if scope == "all" else None
        sort = request.args.get("sort", "taken")
        if sort not in ("taken", "name", "size", "type"):
            sort = "taken"
        order = "desc" if request.args.get("order") == "desc" else "asc"
        page = _int_arg("page", 1, lo=1, hi=MAX_PAGE)
        nav_params = dict(source=scope, type=media_type, sort=sort, order=order)
        if scope == "all":
            nav_params.update(q=query, year=year)
            back = url_for("all_files", q=query, year=year, type=media_type,
                           sort=sort, order=order, page=page)
        else:
            back = url_for("month_gallery", year=row["taken_year"],
                           month=row["taken_month"], type=media_type,
                           sort=sort, order=order, page=page)
        nav_params["page"] = page
        prev_id, next_id = queries.neighbours(
            g.db, file_id, media_type=media_type, scope=scope,
            query=query, year=year, sort=sort, order=order,
        )
        return render_template(
            "viewer.html",
            title=os.path.basename(row["dest_path"] or row["source_path"]),
            f=_decorate(row),
            prev_id=prev_id, next_id=next_id, back=back, nav_params=nav_params,
            **_gallery_context(g.db, media_type=media_type,
                               active={"kind": "viewer"}),
        )

    @app.get("/thumb/<file_id:file_id>.jpg")
    def thumb(file_id: int):
        row = g.db.execute(
            "SELECT dest_path, source_path, sha256, size, mtime, media_type, "
            "duration_ms FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            abort(404)
        if row["media_type"] == "audio":
            abort(415, description="音频没有缩略图")

        path = get_or_create(
            settings, file_id=file_id, dest_path=row["dest_path"],
            source_path=row["source_path"], sha256=row["sha256"],
            size=row["size"], mtime=row["mtime"], media_type=row["media_type"],
            duration_ms=row["duration_ms"],
        )
        if path is None or not path.exists():
            abort(503, description="缩略图生成失败")

        if row["sha256"]:
            # Content-addressed, so the name never goes stale.
            response = send_file(path, mimetype="image/jpeg")
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return response
        return send_file(path, mimetype="image/jpeg")

    @app.get("/media/<file_id:file_id>")
    def media(file_id: int):
        row = g.db.execute(
            "SELECT dest_path, media_type, ext, size FROM files WHERE id=?",
            (file_id,)).fetchone()
        if not row or not row["dest_path"]:
            abort(404)
        path = Path(row["dest_path"])
        if not path.exists():
            abort(404, description="文件不在库中")
        # conditional=True enables Range requests, which is what lets the
        # browser seek inside a <video>.
        return send_file(str(path), conditional=True)

    @app.get("/download/<file_id:file_id>")
    def download(file_id: int):
        row = g.db.execute(
            "SELECT dest_path, source_path FROM files WHERE id=?", (file_id,)).fetchone()
        if not row or not row["dest_path"]:
            abort(404)
        path = Path(row["dest_path"])
        if not path.exists():
            abort(404)
        return send_file(
            str(path), as_attachment=True,
            download_name=os.path.basename(row["source_path"] or path.name),
        )

    @app.get("/quarantine")
    def quarantine_page():
        source = (request.args.get("source") or "").strip() or None
        sources = queries.quarantine_sources(g.db, settings.quarantine_dir)
        if source and source not in sources:
            abort(404)
        rows = queries.quarantine_page(
            g.db, source=source, quarantine_root=settings.quarantine_dir,
        )
        return render_template(
            "quarantine.html",
            title="重复文件（隔离区）",
            files=rows, source=source,
            sources=sources,
            quarantine_dir=settings.quarantine_dir,
            **_gallery_context(g.db, active={"kind": "quarantine"}),
        )

    @app.get("/unsorted")
    def unsorted_page():
        media_type = request.args.get("type") or None
        if media_type not in (None, "image", "video", "audio"):
            media_type = None
        rows = queries.unsorted_page(g.db, media_type=media_type)
        return render_template(
            "unsorted.html",
            title="未分类",
            files=[_decorate(r) for r in rows],
            unsorted_dir=settings.unsorted_dir,
            **_gallery_context(g.db, media_type=media_type,
                               active={"kind": "unsorted"}),
        )

    @app.post("/unsorted/<file_id:file_id>/assign")
    def unsorted_assign(file_id: int):
        row = g.db.execute(
            "SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if not row or row["status"] != "unsorted":
            abort(404)
        try:
            year = int(request.form.get("year", ""))
            month = int(request.form.get("month", ""))
        except (TypeError, ValueError):
            abort(400, description="年月格式不正确")
        if not (1 <= month <= 12) or not (1 <= year <= 9999):
            abort(400, description="年月超出范围")

        src = Path(row["dest_path"] or row["source_path"])
        if not src.exists():
            abort(404, description="文件已不在库中")

        target_dir = settings.library_dir / f"{year:04d}" / f"{year:04d}-{month:02d}"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / src.name
        n = 2
        while target.exists():
            target = target_dir / f"{src.stem}-{n}{src.suffix}"
            n += 1

        result = move_verified(str(src), str(target), expected_sha=row["sha256"])
        if not result.ok:
            abort(500, description=result.error or "移动失败")

        with transaction(g.db) as conn:
            update_file(
                conn, file_id, status="organized", dest_path=str(target),
                taken_year=year, taken_month=month,
                taken_at=f"{year:04d}-{month:02d}-01 00:00:00",
                time_source="manual", time_precision="month",
            )
        return redirect(url_for("unsorted_page"))

    @app.get("/status")
    def status_page():
        data = queries.stats(g.db)
        runs = list(g.db.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 10"))
        return render_template(
            "status.html",
            title="状态总览",
            stats=data, runs=[dict(r) for r in runs],
            data_dir=settings.data_dir,
            **_gallery_context(g.db, active={"kind": "status"}),
        )

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        context = {}
        try:
            context = _gallery_context(g.db, active={"kind": "error"})
        except Exception:
            pass
        return render_template(
            "error.html", title=f"{exc.code} {exc.name}",
            code=exc.code, message=exc.description, **context,
        ), exc.code

    return app


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

#: Ceilings for user-supplied integer query parameters.  SQLite integers are
#: 64-bit but Python's are unbounded, so an absurd ``?page=`` used to reach the
#: query as-is and raise ``OverflowError`` -- a 500 for what should be an empty
#: gallery.  The ceiling also has to cover the derived offset
#: (``(page - 1) * limit``), which overflows long before the page number does.
MAX_PAGE = 1_000_000
MAX_YEAR = 9999

#: An id wider than this cannot be a SQLite rowid, so it cannot exist.
_MAX_FILE_ID_DIGITS = len(str(2 ** 63 - 1))


class FileIdConverter(BaseConverter):
    """``<int:>`` bounded to what SQLite can actually store.

    Flask's integer converter accepts any run of digits and Python ints are
    unbounded, so ``/f/99999999999999999999`` reached the ``WHERE id=?`` lookup
    and raised ``OverflowError`` -- a 500 where a plain 404 belongs, exactly as
    ``/f/-1`` already gives.
    """

    regex = r"\d{1,%d}" % _MAX_FILE_ID_DIGITS

    def to_python(self, value: str) -> int:
        number = int(value)
        if number > 2 ** 63 - 1:
            raise ValidationError()
        return number

    def to_url(self, value: object) -> str:
        return str(int(value))  # type: ignore[arg-type]


def _int_arg(name: str, default: int, *, lo: int, hi: int) -> int:
    """Parse a query-string integer, clamped to ``[lo, hi]``."""
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _gallery_context(
    conn: sqlite3.Connection,
    *,
    media_type: str | None = None,
    sort: str = "taken",
    order: str = "asc",
    active: dict | None = None,
) -> dict:
    """Build the sidebar tree and the filter state shared by every page."""
    tree: dict[int, dict[int, dict[str, int]]] = {}
    for row in conn.execute(queries.TREE_COUNTS):
        year = int(row["taken_year"])
        month = int(row["taken_month"])
        tree.setdefault(year, {}).setdefault(month, {})[row["media_type"]] = int(row["n"])

    extras = conn.execute(queries.SIDEBAR_EXTRAS).fetchone()
    return {
        "tree": tree,
        "extras": {k: int(extras[k]) for k in extras.keys()} if extras else {},
        "media_type": media_type,
        "sort": sort, "order": order,
        "active": active or {},
    }


def _decorate(row: sqlite3.Row) -> dict:
    """Add derived display fields the templates need."""
    d = dict(row)
    dest = d.get("dest_path") or ""
    d["filename"] = os.path.basename(dest) if dest else os.path.basename(
        d.get("source_path") or "")
    d["folder"] = ""
    if d.get("taken_year") and d.get("taken_month"):
        d["folder"] = f"{d['taken_year']}/{d['taken_year']}-{d['taken_month']:02d}"
    taken = d.get("taken_at") or ""
    d["date"] = taken[:10]
    d["clock"] = taken[11:16]
    d["is_image"] = d.get("media_type") == "image"
    d["is_video"] = d.get("media_type") == "video"
    d["is_audio"] = d.get("media_type") == "audio"
    d["resolution"] = (
        f"{d['width']}×{d['height']}" if d.get("width") and d.get("height") else ""
    )
    return d


__all__ = ["create_app"]
