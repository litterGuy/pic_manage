"""SQL used by the web layer, kept in one place so it stays index-friendly."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# sidebar
# --------------------------------------------------------------------------

TREE_COUNTS = """
SELECT taken_year, taken_month, media_type, COUNT(*) AS n
FROM files
WHERE status = 'organized' AND taken_year IS NOT NULL
GROUP BY taken_year, taken_month, media_type
ORDER BY taken_year, taken_month, media_type
"""

SIDEBAR_EXTRAS = """
SELECT
  (SELECT COUNT(*) FROM files WHERE status='unsorted')   AS unsorted,
  (SELECT COUNT(*) FROM files WHERE status='quarantined') AS quarantined,
  (SELECT COUNT(*) FROM files WHERE status='error')       AS errors
"""

# --------------------------------------------------------------------------
# gallery
# --------------------------------------------------------------------------

_MONTH_COLUMNS = """
  id, dest_path, source_path, media_type, ext, size, taken_at, time_source,
  time_precision, width, height, duration_ms, container, codec, thumb_ready
"""

_SORTS = {
    "taken": ("taken_at",),
    "name": ("path_basename(dest_path)",),
    "size": ("size",),
    "type": ("media_type", "taken_at"),
}


def _order_by(sort: str, order: str) -> str:
    direction = "DESC" if order.lower() == "desc" else "ASC"
    fields = _SORTS.get(sort, _SORTS["taken"])
    # Keep missing dates at the end in either direction.
    return ", ".join([*(f"{field} IS NULL ASC, {field} {direction}" for field in fields),
                      f"id {direction}"])


def month_page(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    *,
    media_type: str | None = None,
    sort: str = "taken",
    order: str = "asc",
    limit: int = 120,
    offset: int = 0,
) -> list[sqlite3.Row]:
    order_by = _order_by(sort, order)
    sql = (
        f"SELECT {_MONTH_COLUMNS} FROM files "
        f"WHERE status='organized' AND taken_year=? AND taken_month=? "
        f"AND (? IS NULL OR media_type=?) "
        f"ORDER BY {order_by} "
        f"LIMIT ? OFFSET ?"
    )
    return list(conn.execute(sql, (year, month, media_type, media_type, limit, offset)))


def month_total(
    conn: sqlite3.Connection, year: int, month: int, media_type: str | None = None
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM files WHERE status='organized' AND taken_year=? "
        "AND taken_month=? AND (? IS NULL OR media_type=?)",
        (year, month, media_type, media_type),
    ).fetchone()
    return int(row[0]) if row else 0


# --------------------------------------------------------------------------
# flat list / search
# --------------------------------------------------------------------------

def all_page(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    media_type: str | None = None,
    year: int | None = None,
    sort: str = "taken",
    order: str = "desc",
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[sqlite3.Row], int]:
    order_by = _order_by(sort, order)

    where = ["status='organized'"]
    params: list[Any] = []
    if media_type:
        where.append("media_type=?")
        params.append(media_type)
    if year:
        where.append("taken_year=?")
        params.append(year)
    if query:
        # LIKE with an explicit escape so a literal % or _ in the query cannot
        # turn into a wildcard.
        where.append("(dest_path LIKE ? ESCAPE '\\' OR source_path LIKE ? ESCAPE '\\')")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.extend([f"%{escaped}%", f"%{escaped}%"])

    clause = " AND ".join(where)
    total_row = conn.execute(f"SELECT COUNT(*) FROM files WHERE {clause}", params).fetchone()
    total = int(total_row[0]) if total_row else 0
    rows = list(conn.execute(
        f"SELECT {_MONTH_COLUMNS} FROM files WHERE {clause} "
        f"ORDER BY {order_by} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ))
    return rows, total


def years_with_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT taken_year AS y, COUNT(*) AS n FROM files "
        "WHERE status='organized' AND taken_year IS NOT NULL "
        "GROUP BY taken_year ORDER BY taken_year DESC"))


# --------------------------------------------------------------------------
# viewer navigation
# --------------------------------------------------------------------------

def neighbours(
    conn: sqlite3.Connection,
    file_id: int,
    *,
    media_type: str | None = None,
    scope: str = "month",
    query: str | None = None,
    year: int | None = None,
    sort: str = "taken",
    order: str = "asc",
) -> tuple[int | None, int | None]:
    """Adjacent ids in the exact filtered and sorted gallery being viewed."""
    row = conn.execute(
        "SELECT taken_year, taken_month, taken_at FROM files WHERE id=?", (file_id,)
    ).fetchone()
    if not row:
        return None, None
    where = ["status='organized'"]
    params: list[Any] = []
    if scope == "month":
        if row["taken_year"] is None or row["taken_month"] is None:
            return None, None
        where.extend(("taken_year=?", "taken_month=?"))
        params.extend((row["taken_year"], row["taken_month"]))
    elif year:
        where.append("taken_year=?")
        params.append(year)
    if media_type:
        where.append("media_type=?")
        params.append(media_type)
    if scope == "all" and query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(dest_path LIKE ? ESCAPE '\\' OR source_path LIKE ? ESCAPE '\\')")
        params.extend((f"%{escaped}%", f"%{escaped}%"))
    result = conn.execute(
        "WITH ordered AS (SELECT id, "
        f"LAG(id) OVER (ORDER BY {_order_by(sort, order)}) AS prev_id, "
        f"LEAD(id) OVER (ORDER BY {_order_by(sort, order)}) AS next_id "
        f"FROM files WHERE {' AND '.join(where)}) "
        "SELECT prev_id, next_id FROM ordered WHERE id=?",
        (*params, file_id),
    ).fetchone()
    return (result["prev_id"], result["next_id"]) if result else (None, None)


# --------------------------------------------------------------------------
# quarantine / unsorted / status
# --------------------------------------------------------------------------

def quarantine_page(
    conn: sqlite3.Connection, *, source: str | None = None,
    quarantine_root: str | Path | None = None, limit: int = 500
) -> list[sqlite3.Row]:
    sql = ("SELECT id, source_path, dest_path, size, media_type, ext, taken_at, "
           "time_source, time_precision, sha256, dup_kind, dup_of "
           "FROM files WHERE status='quarantined'")
    params: list[Any] = []
    if source:
        if quarantine_root is None:
            raise ValueError("quarantine_root is required when filtering by source")
        sql += " AND dest_path LIKE ? ESCAPE '\\'"
        prefix = str(Path(quarantine_root) / source) + os.sep
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"{escaped}%")
    sql += " ORDER BY dest_path LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))


def quarantine_sources(conn: sqlite3.Connection,
                       quarantine_dir: str | Path) -> list[str]:
    """Distinct source keys present in the quarantine tree.

    A quarantined file lives at ``<quarantine_root>/<source_key>/<relpath>``, so
    the key is the first path component *under the quarantine root*.

    The root has to be passed in rather than searched for in the path: a
    preserved relpath may itself contain a folder called ``quarantine``, and a
    duplicate of ``D:\\photos\\quarantine\\old\\a.jpg`` (source ``D:\\photos``)
    would then be reported under the bogus key ``old`` -- a dropdown entry that
    filters to exactly one unrelated file.
    """
    root = os.path.normcase(os.path.normpath(str(quarantine_dir))).rstrip("\\/")

    out: list[str] = []
    seen: set[str] = set()
    for row in conn.execute(
            "SELECT DISTINCT dest_path FROM files WHERE status='quarantined' "
            "AND dest_path IS NOT NULL ORDER BY dest_path"):
        dest = os.path.normcase(os.path.normpath(str(row[0])))
        if not dest.startswith(root + "\\"):
            continue
        parts = [p for p in dest[len(root) + 1:].split("\\") if p]
        key = parts[0] if parts else ""
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def unsorted_page(
    conn: sqlite3.Connection, *, media_type: str | None = None, limit: int = 500
) -> list[sqlite3.Row]:
    sql = ("SELECT id, source_path, dest_path, size, media_type, ext, width, height, "
           "duration_ms, container, codec, thumb_ready "
           "FROM files WHERE status='unsorted'")
    params: list[Any] = []
    if media_type:
        sql += " AND media_type=?"
        params.append(media_type)
    sql += " ORDER BY media_type, source_path LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def scalar(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row else 0

    by_type = {row[0]: row[1] for row in conn.execute(
        "SELECT media_type, COUNT(*) FROM files WHERE status='organized' "
        "GROUP BY media_type")}
    by_status = {row[0]: row[1] for row in conn.execute(
        "SELECT status, COUNT(*) FROM files GROUP BY status")}
    by_source = {row[0]: row[1] for row in conn.execute(
        "SELECT time_source, COUNT(*) FROM files "
        "WHERE status='organized' GROUP BY time_source ORDER BY COUNT(*) DESC")}
    months = [(row[0], row[1]) for row in conn.execute(
        "SELECT DISTINCT taken_year, taken_month FROM files "
        "WHERE status='organized' AND taken_year IS NOT NULL "
        "ORDER BY taken_year DESC, taken_month DESC")]
    return {
        "organized": by_status.get("organized", 0),
        "unsorted": by_status.get("unsorted", 0),
        "quarantined": by_status.get("quarantined", 0),
        "error": by_status.get("error", 0),
        "by_type": by_type,
        "by_source": by_source,
        "months": months,
        "bytes": scalar("SELECT COALESCE(SUM(size),0) FROM files WHERE status='organized'"),
    }


def newest_month(conn: sqlite3.Connection) -> tuple[int, int] | None:
    row = conn.execute(
        "SELECT taken_year, taken_month FROM files "
        "WHERE status='organized' AND taken_year IS NOT NULL "
        "ORDER BY taken_year DESC, taken_month DESC LIMIT 1"
    ).fetchone()
    return (int(row[0]), int(row[1])) if row else None


__all__ = [
    "TREE_COUNTS", "SIDEBAR_EXTRAS", "month_page", "month_total", "all_page",
    "years_with_counts", "neighbours", "quarantine_page", "quarantine_sources",
    "unsorted_page", "stats", "newest_month",
]
