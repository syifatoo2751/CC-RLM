"""
Persistent SQLite store — import graph + relevance scores.

Replaces the ephemeral /tmp JSON files used by relevance_store.py and
the in-memory-only RepoIndex. Now scores and graph edges survive server
restarts, making the system smarter from the very first query after reboot.

Two tables:
  import_graph — per-file import/imported_by edges + mtime
  relevance    — per-file citation hit/miss counts

Default path: ~/.cc-rlm/store.db
Override: RLM_STORE_PATH env var.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from threading import Lock

log = logging.getLogger("rlm.store")

DEFAULT_DB = Path.home() / ".cc-rlm" / "store.db"

_conn: sqlite3.Connection | None = None
_lock = Lock()


def open_db(path: Path = DEFAULT_DB) -> None:
    global _conn
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")  # concurrent reads while writing
    _conn.execute("PRAGMA synchronous=NORMAL")  # fast writes, safe enough
    _init_schema()
    log.info("SQLite store opened: %s", path)


def close_db() -> None:
    global _conn
    if _conn:
        _conn.close()
        _conn = None
        log.info("SQLite store closed")


def _init_schema() -> None:
    with _lock:
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS import_graph (
                repo_path   TEXT NOT NULL,
                file_path   TEXT NOT NULL,
                imports     TEXT NOT NULL DEFAULT '[]',
                imported_by TEXT NOT NULL DEFAULT '[]',
                mtime       REAL NOT NULL DEFAULT 0.0,
                PRIMARY KEY (repo_path, file_path)
            );
            CREATE TABLE IF NOT EXISTS relevance (
                repo_path TEXT NOT NULL,
                file_path TEXT NOT NULL,
                hits      INTEGER NOT NULL DEFAULT 0,
                total     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (repo_path, file_path)
            );
        """)
        _conn.commit()


# ------------------------------------------------------------------
# Import graph persistence
# ------------------------------------------------------------------

def load_import_graph(repo_path: str) -> dict[str, dict]:
    """
    Load import graph for a repo from SQLite.
    Returns {file_path: {imports: [...], imported_by: [...], mtime: float}}.
    Returns {} if store not open.
    """
    if not _conn:
        return {}
    with _lock:
        rows = _conn.execute(
            "SELECT file_path, imports, imported_by, mtime FROM import_graph WHERE repo_path = ?",
            (repo_path,),
        ).fetchall()
    result = {}
    for row in rows:
        result[row["file_path"]] = {
            "imports": json.loads(row["imports"]),
            "imported_by": json.loads(row["imported_by"]),
            "mtime": row["mtime"],
        }
    log.debug("Loaded import graph: %d files for %s", len(result), repo_path)
    return result


def save_file_graph(
    repo_path: str,
    file_path: str,
    imports: list[str],
    imported_by: list[str],
    mtime: float,
) -> None:
    """Upsert one file's graph entry. Called whenever a file is re-indexed."""
    if not _conn:
        return
    with _lock:
        _conn.execute(
            """INSERT INTO import_graph (repo_path, file_path, imports, imported_by, mtime)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(repo_path, file_path) DO UPDATE SET
                   imports     = excluded.imports,
                   imported_by = excluded.imported_by,
                   mtime       = excluded.mtime""",
            (repo_path, file_path, json.dumps(imports), json.dumps(imported_by), mtime),
        )
        _conn.commit()


# ------------------------------------------------------------------
# Relevance persistence
# ------------------------------------------------------------------

def load_relevance(repo_path: str) -> dict[str, dict]:
    """
    Load relevance scores for a repo from SQLite.
    Returns {file_path: {hits: int, total: int}}.
    """
    if not _conn:
        return {}
    with _lock:
        rows = _conn.execute(
            "SELECT file_path, hits, total FROM relevance WHERE repo_path = ?",
            (repo_path,),
        ).fetchall()
    return {row["file_path"]: {"hits": row["hits"], "total": row["total"]} for row in rows}


def save_relevance(repo_path: str, file_path: str, hits: int, total: int) -> None:
    """Upsert relevance counts for one file. Called after each feedback turn."""
    if not _conn:
        return
    with _lock:
        _conn.execute(
            """INSERT INTO relevance (repo_path, file_path, hits, total)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(repo_path, file_path) DO UPDATE SET
                   hits  = excluded.hits,
                   total = excluded.total""",
            (repo_path, file_path, hits, total),
        )
        _conn.commit()


def db_stats() -> dict:
    """Row counts for health endpoint."""
    if not _conn:
        return {"store": "not open"}
    with _lock:
        ig = _conn.execute("SELECT COUNT(*) FROM import_graph").fetchone()[0]
        rel = _conn.execute("SELECT COUNT(*) FROM relevance").fetchone()[0]
    return {"import_graph_rows": ig, "relevance_rows": rel}
