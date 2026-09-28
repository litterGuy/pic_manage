"""SQLite storage: schema, connection factory, upserts and audit journal.

WAL mode is essential: it lets the Flask app read while a scan writes, and it
survives an interrupted process without corrupting the index.
"""

from __future__ import annotations

import json
import logging
import ntpath
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Upper bound on parameters in one ``IN (...)`` clause.  SQLite's
#: ``SQLITE_MAX_VARIABLE_NUMBER`` has been 999 historically and 32766 on recent
#: builds; 500 is safe on both and keeps the chunked queries readable.
SQL_VAR_CHUNK = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at     TEXT    NOT NULL,
  finished_at    TEXT,
  source_roots   TEXT    NOT NULL,
  argv           TEXT    NOT NULL,
  mode           TEXT    NOT NULL CHECK (mode IN ('copy','move','dry-run')),
  options        TEXT    NOT NULL,
  interrupted    INTEGER NOT NULL DEFAULT 0,
  files_seen     INTEGER NOT NULL DEFAULT 0,
  files_skipped  INTEGER NOT NULL DEFAULT 0,
  files_probed   INTEGER NOT NULL DEFAULT 0,
  organized      INTEGER NOT NULL DEFAULT 0,
  unsorted       INTEGER NOT NULL DEFAULT 0,
  quarantined    INTEGER NOT NULL DEFAULT 0,
  exact_groups   INTEGER NOT NULL DEFAULT 0,
  phash_groups   INTEGER NOT NULL DEFAULT 0,
  errors         INTEGER NOT NULL DEFAULT 0,
  duration_s     REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

CREATE TABLE IF NOT EXISTS files (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  source_path    TEXT    NOT NULL,
  dest_path      TEXT,
  status         TEXT    NOT NULL CHECK (status IN
                  ('organized','unsorted','quarantined','skipped','error')),
  media_type     TEXT    NOT NULL CHECK (media_type IN ('image','video','audio','other')),
  ext            TEXT    NOT NULL,
  size           INTEGER NOT NULL,
  mtime          REAL    NOT NULL,
  sha256         TEXT,
  phash          TEXT,
  taken_at       TEXT,
  taken_year     INTEGER,
  taken_month    INTEGER,
  time_source    TEXT    NOT NULL,
  time_raw       TEXT,
  time_precision TEXT    NOT NULL DEFAULT 'none',
  width          INTEGER,
  height         INTEGER,
  duration_ms    INTEGER,
  container      TEXT,
  codec          TEXT,
  thumb_ready    INTEGER NOT NULL DEFAULT 0,
  dup_of         INTEGER REFERENCES files(id) ON DELETE SET NULL,
  dup_kind       TEXT CHECK (dup_kind IN ('exact','phash')),
  dup_group      TEXT,
  skip_reason    TEXT,
  error          TEXT,
  first_seen_run INTEGER NOT NULL REFERENCES runs(id),
  first_seen_at  TEXT    NOT NULL,
  last_seen_run  INTEGER NOT NULL REFERENCES runs(id),
  last_seen_at   TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_files_source ON files(source_path);
CREATE INDEX IF NOT EXISTS idx_files_sha      ON files(sha256) WHERE sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_files_status   ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_type     ON files(media_type);
CREATE INDEX IF NOT EXISTS idx_files_taken    ON files(taken_at);
CREATE INDEX IF NOT EXISTS idx_files_ym       ON files(taken_year, taken_month);
CREATE INDEX IF NOT EXISTS idx_files_ym_type  ON files(taken_year, taken_month, media_type, status);
CREATE INDEX IF NOT EXISTS idx_files_dest     ON files(dest_path) WHERE dest_path IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_files_dupof    ON files(dup_of) WHERE dup_of IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_files_phash    ON files(phash) WHERE phash IS NOT NULL;

CREATE TABLE IF NOT EXISTS actions (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id    INTEGER NOT NULL REFERENCES runs(id),
  file_id   INTEGER REFERENCES files(id) ON DELETE SET NULL,
  ts        TEXT    NOT NULL,
  action    TEXT    NOT NULL,
  from_path TEXT,
  to_path   TEXT,
  detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_run  ON actions(run_id);
CREATE INDEX IF NOT EXISTS idx_actions_file ON actions(file_id);

CREATE TABLE IF NOT EXISTS source_keys (
  key        TEXT PRIMARY KEY,
  root_path  TEXT NOT NULL,
  first_seen TEXT NOT NULL
);
"""

UPSERT_COLUMNS = (
    "source_path", "dest_path", "status", "media_type", "ext", "size", "mtime",
    "sha256", "phash", "taken_at", "taken_year", "taken_month", "time_source",
    "time_raw", "time_precision", "width", "height", "duration_ms", "container",
    "codec", "thumb_ready", "dup_of", "dup_kind", "dup_group", "skip_reason",
    "error", "first_seen_run", "first_seen_at", "last_seen_run", "last_seen_at",
)


def _text_factory(value: bytes) -> str:
    """Decode tolerant of unpaired surrogates from malformed UTF-16 names.

    A filename produced by another OS can carry bytes that ``os.fsdecode``
    turns into lone surrogates; letting those reach sqlite3 raises
    ``UnicodeEncodeError`` and would abort an entire scan.
    """
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value.decode("utf-8", "surrogateescape").encode(
            "utf-8", "replace").decode("utf-8", "replace")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str | Path, *, create: bool = True) -> sqlite3.Connection:
    """Open a connection configured for concurrent scan/read access."""
    path = Path(db_path)
    if not create and not path.exists():
        raise FileNotFoundError(f"database not found: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.text_factory = _text_factory
    conn.create_function("path_basename", 1, lambda path: ntpath.basename(path or ""), deterministic=True)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-16000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema if needed and stamp the version."""
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction with ``BEGIN IMMEDIATE`` to avoid upgrade deadlocks."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def open_db(db_path: str | Path, *, create: bool = True) -> sqlite3.Connection:
    conn = connect(db_path, create=create)
    if create:
        init_db(conn)
    return conn


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

def create_run(
    conn: sqlite3.Connection,
    *,
    source_roots: Sequence[str],
    argv: Sequence[str],
    mode: str,
    options: dict[str, Any],
) -> int:
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO runs(started_at, source_roots, argv, mode, options) "
            "VALUES (?,?,?,?,?)",
            (
                utcnow(),
                json.dumps([str(r) for r in source_roots], ensure_ascii=False),
                " ".join(str(a) for a in argv),
                mode,
                json.dumps(options, ensure_ascii=False, default=str),
            ),
        )
        return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    interrupted: bool = False,
    **counts: Any,
) -> None:
    duration_s = counts.pop("duration_s", None)
    sets = ["finished_at=?", "interrupted=?"]
    params: list[Any] = [utcnow(), 1 if interrupted else 0]
    for key, value in counts.items():
        sets.append(f"{key}=?")
        params.append(value)
    if duration_s is not None:
        sets.append("duration_s=?")
        params.append(duration_s)
    params.append(run_id)
    conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", params)


def close_stale_runs(conn: sqlite3.Connection) -> int:
    """Mark runs left open by a crash as interrupted (called at scan start)."""
    now = utcnow()
    cur = conn.execute(
        "UPDATE runs SET interrupted=1, finished_at=COALESCE(finished_at, ?) "
        "WHERE finished_at IS NULL",
        (now,),
    )
    return cur.rowcount or 0


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ))


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------

def upsert_file(conn: sqlite3.Connection, run_id: int, row: dict[str, Any]) -> int:
    """Insert or update a ``files`` row keyed on ``source_path``.

    ``first_seen_*`` is preserved across updates so the original sighting stays
    auditable; ``last_seen_*`` always reflects the current run.

    Returns the row id.  Note that ``lastrowid`` is meaningless after the
    UPDATE branch of an upsert, so the id is read back explicitly.
    """
    cols = [c for c in UPSERT_COLUMNS if c in row]
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(
        f"{c}=excluded.{c}" for c in cols
        if c not in ("first_seen_run", "first_seen_at")
    )
    sql = (
        f"INSERT INTO files ({', '.join(cols)}, first_seen_run, first_seen_at, "
        f"last_seen_run, last_seen_at) VALUES ({placeholders}, ?, ?, ?, ?) "
        f"ON CONFLICT(source_path) DO UPDATE SET {updates}"
    )
    params = [row[c] for c in cols]
    params += [run_id, utcnow(), run_id, utcnow()]
    conn.execute(sql, params)
    found = conn.execute(
        "SELECT id FROM files WHERE source_path=?", (row["source_path"],)
    ).fetchone()
    return int(found["id"]) if found else -1


def update_file(conn: sqlite3.Connection, file_id: int, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE files SET {sets} WHERE id=?", (*fields.values(), file_id))


def get_file(conn: sqlite3.Connection, file_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()


def get_files(conn: sqlite3.Connection, file_ids: Iterable[int]) -> list[sqlite3.Row]:
    ids = list(file_ids)
    if not ids:
        return []
    out: list[sqlite3.Row] = []
    for start in range(0, len(ids), SQL_VAR_CHUNK):
        batch = ids[start:start + SQL_VAR_CHUNK]
        marks = ", ".join("?" for _ in batch)
        out.extend(conn.execute(f"SELECT * FROM files WHERE id IN ({marks})", batch))
    return out


def indexed_snapshot(conn: sqlite3.Connection) -> dict[str, tuple[int, float, str]]:
    """``source_path -> (size, mtime, status)`` for the resume stage.

    One query; the whole point is to avoid touching the filesystem for files we
    have already processed in an earlier run.
    """
    out: dict[str, tuple[int, float, str]] = {}
    for row in conn.execute("SELECT source_path, size, mtime, status FROM files"):
        out[row["source_path"]] = (row["size"], row["mtime"], row["status"])
    return out


def sha256_owners(conn: sqlite3.Connection, hashes: Iterable[str]) -> dict[str, sqlite3.Row]:
    """First library row for each hash -- used so re-scans don't re-quarantine.

    Chunked because SQLite caps the number of bound parameters per statement
    (``SQLITE_MAX_VARIABLE_NUMBER``); a scan of a large library hands us tens of
    thousands of hashes at once and a single ``IN (...)`` would raise
    ``sqlite3.OperationalError: too many SQL variables``.
    """
    wanted = {h for h in hashes if h}
    if not wanted:
        return {}
    out: dict[str, sqlite3.Row] = {}
    ordered = sorted(wanted)
    for start in range(0, len(ordered), SQL_VAR_CHUNK):
        batch = ordered[start:start + SQL_VAR_CHUNK]
        marks = ", ".join("?" for _ in batch)
        for row in conn.execute(
            f"SELECT * FROM files WHERE sha256 IN ({marks}) "
            f"AND status IN ('organized','unsorted') ORDER BY id",
            tuple(batch),
        ):
            out.setdefault(row["sha256"], row)
    return out


def log_action(
    conn: sqlite3.Connection,
    run_id: int,
    file_id: int | None,
    action: str,
    from_path: str | None = None,
    to_path: str | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO actions(run_id, file_id, ts, action, from_path, to_path, detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (run_id, file_id, utcnow(), action, from_path, to_path, detail),
    )


def reserve_source_key(conn: sqlite3.Connection, root: str) -> str:
    """Stable sanitized key for a source root, disambiguated on collision."""
    from .scanner.classify import sanitize_source_key

    base = sanitize_source_key(root)
    existing = {
        row["key"]: row["root_path"]
        for row in conn.execute("SELECT key, root_path FROM source_keys")
    }
    if base in existing:
        if existing[base] == root:
            return base
        n = 2
        while f"{base}-{n}" in existing and existing[f"{base}-{n}"] != root:
            n += 1
        base = f"{base}-{n}"
    conn.execute(
        "INSERT INTO source_keys(key, root_path, first_seen) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET root_path=excluded.root_path",
        (base, root, utcnow()),
    )
    return base


def source_keys(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["root_path"] for row in conn.execute("SELECT key, root_path FROM source_keys")}


__all__ = [
    "SCHEMA", "SCHEMA_VERSION", "connect", "init_db", "open_db", "transaction",
    "create_run", "finish_run", "close_stale_runs", "recent_runs",
    "upsert_file", "update_file", "get_file", "get_files", "indexed_snapshot",
    "sha256_owners", "log_action", "reserve_source_key", "source_keys", "utcnow",
]
