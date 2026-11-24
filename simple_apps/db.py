"""SQLite helpers shared by the registry and the per-app data stores."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection in autocommit mode; callers open transactions explicitly.

    A connection per operation keeps threads independent; SQLite handles the
    cross-connection locking (WAL + busy timeout).
    """
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(path: Path, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """Run a block in one transaction: commit on success, roll back on error."""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def init_schema(path: Path, schema: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(schema)
    finally:
        conn.close()
