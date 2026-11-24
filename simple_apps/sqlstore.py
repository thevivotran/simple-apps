"""App-declared SQL: migrations and named queries on apps/<app>/data.db
(ARCHITECTURE.md §3.10, §4.13).

A bundle may ship `db/migrations/<n>_<name>.sql` and `db/queries.sql`. The browser
never sends SQL: it calls a query by name with JSON parameters. Every statement
runs under an SQLite authorizer that only allows what that context needs, so app
SQL can never attach another database, change pragmas, touch the platform's own
tables (records, kv, _migrations) or — outside migrations — change the schema.
"""

import base64
import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import names
from .datastore import AppData
from .db import connect, transaction, utc_now
from .errors import ApiError

MIGRATION_RE = re.compile(r"(\d{1,9})_[A-Za-z0-9_-]{1,64}\.sql")
NAME_LINE_RE = re.compile(r"--\s*name:\s*(\S+)\s*(\(write\))?\s*")
COMMENTS_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
PLATFORM_OBJECTS = frozenset({"records", "kv", "_migrations", "records_by_time"})
MAX_ROWS = 1000
QUERY_SECONDS = 2.0

_A = sqlite3
ALWAYS = {_A.SQLITE_SELECT, _A.SQLITE_READ, _A.SQLITE_RECURSIVE}
WRITES = {_A.SQLITE_INSERT, _A.SQLITE_UPDATE, _A.SQLITE_DELETE}
DDL = {
    _A.SQLITE_CREATE_TABLE,
    _A.SQLITE_CREATE_INDEX,
    _A.SQLITE_CREATE_TRIGGER,
    _A.SQLITE_CREATE_VIEW,
    _A.SQLITE_DROP_TABLE,
    _A.SQLITE_DROP_INDEX,
    _A.SQLITE_DROP_TRIGGER,
    _A.SQLITE_DROP_VIEW,
    _A.SQLITE_ALTER_TABLE,
    _A.SQLITE_REINDEX,
    _A.SQLITE_ANALYZE,
}


@dataclass(frozen=True)
class Query:
    name: str
    write: bool
    statements: tuple[str, ...]


@dataclass(frozen=True)
class Migration:
    number: int
    name: str
    sha256: str
    statements: tuple[str, ...]


@dataclass(frozen=True)
class DbBundle:
    migrations: tuple[Migration, ...] = ()
    queries: tuple[Query, ...] = ()


# -- parsing ------------------------------------------------------------------


def _is_blank(sql: str) -> bool:
    return not COMMENTS_RE.sub("", sql).replace(";", "").strip()


def split_statements(sql: str) -> list[str]:
    """Split a script into single statements (trigger bodies stay whole)."""
    statements = []
    start = 0
    for i, ch in enumerate(sql):
        if ch == ";" and sqlite3.complete_statement(sql[start : i + 1]):
            chunk = sql[start : i + 1]
            if not _is_blank(chunk):
                statements.append(chunk.strip())
            start = i + 1
    rest = sql[start:]
    if not _is_blank(rest):
        statements.append(rest.strip())
    return statements


def parse_queries(text: str) -> tuple[Query, ...]:
    """Parse queries.sql: each query starts with `-- name: <name>` or `-- name: <name> (write)`."""
    queries: list[Query] = []
    current: tuple[str, bool] | None = None
    body: list[str] = []

    def flush() -> None:
        if current is None:
            if not _is_blank("\n".join(body)):
                raise ApiError(400, "db/queries.sql: SQL before the first '-- name:' line")
            return
        statements = split_statements("\n".join(body))
        if not statements:
            raise ApiError(400, f"db/queries.sql: query {current[0]!r} is empty")
        queries.append(Query(current[0], current[1], tuple(statements)))

    for line in text.splitlines():
        m = NAME_LINE_RE.fullmatch(line.strip())
        if m is None:
            body.append(line)
            continue
        flush()
        name = m.group(1)
        if not names.is_query_name(name):
            raise ApiError(400, f"db/queries.sql: invalid query name {name!r}")
        if any(q.name == name for q in queries):
            raise ApiError(400, f"db/queries.sql: duplicate query {name!r}")
        current, body = (name, m.group(2) is not None), []
    flush()
    return tuple(queries)


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ApiError(400, f"{label} is not valid UTF-8") from None


def read_bundle(site: Path) -> DbBundle:
    """Read and parse db/ from an extracted bundle (SQL is checked later, in migrate())."""
    db_dir = site / "db"
    migrations: list[Migration] = []
    mig_dir = db_dir / "migrations"
    if mig_dir.is_dir():
        for path in mig_dir.iterdir():
            m = MIGRATION_RE.fullmatch(path.name)
            if m is None or not path.is_file():
                raise ApiError(400, f"db/migrations/{path.name}: expected <number>_<name>.sql")
            raw = path.read_bytes()
            text = _read_text(path, f"db/migrations/{path.name}")
            sha = hashlib.sha256(raw).hexdigest()
            migrations.append(Migration(int(m.group(1)), path.name, sha, tuple(split_statements(text))))
        migrations.sort(key=lambda mig: mig.number)
        for a, b in zip(migrations, migrations[1:], strict=False):
            if a.number == b.number:
                raise ApiError(400, f"db/migrations: {a.name} and {b.name} share number {a.number}")
    queries: tuple[Query, ...] = ()
    qfile = db_dir / "queries.sql"
    if qfile.is_file():
        queries = parse_queries(_read_text(qfile, "db/queries.sql"))
    return DbBundle(tuple(migrations), queries)


@lru_cache(maxsize=64)
def _cached_queries(path: str, mtime_ns: int, size: int) -> dict[str, Query]:
    return {q.name: q for q in parse_queries(Path(path).read_text(encoding="utf-8"))}


def load_queries(site_current: Path) -> dict[str, Query]:
    """Queries of the version `site/current` points at (so rollback reverts them too)."""
    path = (site_current / "db" / "queries.sql").resolve()
    try:
        st = path.stat()
    except FileNotFoundError:
        return {}
    return _cached_queries(str(path), st.st_mtime_ns, st.st_size)


# -- sandbox ------------------------------------------------------------------


class Sandbox:
    """Authorizer + time limit for one block of app SQL on a connection."""

    def __init__(self, *, ddl: bool, write: bool):
        self.ddl = ddl
        self.write = write
        self.denied: str | None = None

    def _deny(self, reason: str) -> int:
        self.denied = self.denied or reason
        return sqlite3.SQLITE_DENY

    def authorize(self, action: int, arg1: str | None, arg2: str | None, _db: Any, _src: Any) -> int:
        if action in ALWAYS:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            if arg2 == "load_extension":
                return self._deny("load_extension is not allowed")
            return sqlite3.SQLITE_OK
        if action in WRITES:
            if not self.write:
                return self._deny(f"writes to {arg1}; mark the query '(write)'")
            if arg1 in PLATFORM_OBJECTS:
                return self._deny(f"{arg1} is a platform table; use the collection/kv API")
            return sqlite3.SQLITE_OK
        if action in DDL:
            if not self.ddl:
                return self._deny("schema changes belong in db/migrations")
            if arg1 in PLATFORM_OBJECTS or arg2 in PLATFORM_OBJECTS:
                return self._deny(f"{arg2 if arg2 in PLATFORM_OBJECTS else arg1} is a platform object")
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA:
            return self._deny(f"PRAGMA {arg1} is not allowed")
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return self._deny("ATTACH / DETACH are not allowed")
        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
            return self._deny("transactions are managed by the platform")
        return self._deny("statement type is not allowed (temp objects and virtual tables are unsupported)")

    @contextmanager
    def on(self, conn: sqlite3.Connection, seconds: float | None = None) -> Iterator[None]:
        conn.set_authorizer(self.authorize)
        if seconds is not None:
            deadline = time.monotonic() + seconds
            conn.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        try:
            yield
        finally:
            # Cleared before the caller's COMMIT / ROLLBACK, which the authorizer would deny.
            conn.set_authorizer(None)
            conn.set_progress_handler(None, 0)

    def explain(self, err: sqlite3.Error) -> str:
        return f"not allowed: {self.denied}" if self.denied and "not authorized" in str(err) else str(err)


class _AnyParams(dict):
    """Binds NULL for every named parameter, so a statement can be prepared via EXPLAIN."""

    def __missing__(self, key: str) -> None:
        return None


# -- migrations (deploy time) -------------------------------------------------


def applied_migrations(path: Path) -> dict[str, str]:
    conn = connect(path)
    try:
        return {r["name"]: r["sha256"] for r in conn.execute("SELECT name, sha256 FROM _migrations")}
    finally:
        conn.close()


def pending_migrations(path: Path, bundle: DbBundle) -> list[Migration]:
    """Migrations not yet applied; applied ones must still be in the bundle, unchanged."""
    applied = applied_migrations(path)
    by_name = {m.name: m for m in bundle.migrations}
    for name, sha in applied.items():
        if name not in by_name:
            raise ApiError(400, f"db/migrations/{name} was already applied and must stay in the bundle")
        if by_name[name].sha256 != sha:
            raise ApiError(400, f"db/migrations/{name} was changed after being applied; add a new migration")
    pending = [m for m in bundle.migrations if m.name not in applied]
    if applied and pending:
        last = max(by_name[n].number for n in applied)
        early = [m.name for m in pending if m.number < last]
        if early:
            raise ApiError(400, f"db/migrations/{early[0]} is numbered before an already-applied migration")
    return pending


def migrate(path: Path, bundle: DbBundle, pending: list[Migration]) -> None:
    """Apply pending migrations and prove every query prepares, in one transaction.

    Any failure rolls the whole thing back, so the schema is unchanged.
    """
    with transaction(path, immediate=True) as conn:
        for mig in pending:
            box = Sandbox(ddl=True, write=True)
            for stmt in mig.statements:
                try:
                    with box.on(conn):
                        conn.execute(stmt).fetchall()
                except sqlite3.Error as err:
                    raise ApiError(400, f"db/migrations/{mig.name} failed: {box.explain(err)}") from None
            conn.execute(
                "INSERT INTO _migrations (name, sha256, applied_at) VALUES (?, ?, ?)",
                (mig.name, mig.sha256, utc_now()),
            )
        for query in bundle.queries:
            box = Sandbox(ddl=False, write=query.write)
            for stmt in query.statements:
                try:
                    with box.on(conn):
                        conn.execute("EXPLAIN " + stmt, _AnyParams()).fetchall()
                except sqlite3.Error as err:
                    raise ApiError(400, f"db/queries.sql: query {query.name!r}: {box.explain(err)}") from None


# -- queries (request time) ---------------------------------------------------


def _param(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def _cell(value: Any) -> Any:
    return base64.b64encode(value).decode() if isinstance(value, bytes) else value


def _run(conn: sqlite3.Connection, query: Query, params: dict) -> tuple[list[dict], bool]:
    rows: list[dict] = []
    truncated = False
    for stmt in query.statements:
        cur = conn.execute(stmt, params)
        try:
            if cur.description is not None:
                cols = [d[0] for d in cur.description]
                fetched = cur.fetchmany(MAX_ROWS + 1)
                truncated = len(fetched) > MAX_ROWS
                rows = [{c: _cell(v) for c, v in zip(cols, r, strict=True)} for r in fetched[:MAX_ROWS]]
        finally:
            cur.close()
    return rows, truncated


def run_query(store: AppData, site_current: Path, name: str, params: Any) -> dict:
    """Run a named query. Returns {"rows", "changes", "truncated"}."""
    if not names.is_query_name(name):
        raise ApiError(400, "invalid query name")
    query = load_queries(site_current).get(name)
    if query is None:
        raise ApiError(404, "query not found")
    if not isinstance(params, dict):
        raise ApiError(400, "query parameters must be a JSON object")
    bound = {k: _param(v) for k, v in params.items()}
    box = Sandbox(ddl=False, write=query.write)
    try:
        if query.write:
            store.check_quota()
            with transaction(store.path, immediate=True) as conn:
                before = conn.total_changes
                with box.on(conn, QUERY_SECONDS):
                    rows, truncated = _run(conn, query, bound)
                changes = conn.total_changes - before
        else:
            conn = connect(store.path)
            try:
                conn.execute("PRAGMA query_only = ON")
                conn.execute("BEGIN")  # one snapshot for all statements
                with box.on(conn, QUERY_SECONDS):
                    rows, truncated = _run(conn, query, bound)
                conn.execute("COMMIT")
                changes = 0
            finally:
                conn.close()
    except sqlite3.IntegrityError as err:
        raise ApiError(409, str(err)) from None
    except sqlite3.ProgrammingError as err:
        raise ApiError(400, str(err)) from None
    except sqlite3.OperationalError as err:
        msg = str(err)
        if msg == "interrupted":
            raise ApiError(503, f"query took longer than {QUERY_SECONDS:g} s") from None
        if "locked" in msg or "busy" in msg:
            raise ApiError(503, "database busy, retry") from None
        raise ApiError(400, box.explain(err)) from None
    except sqlite3.Error as err:
        raise ApiError(400, box.explain(err)) from None
    return {"rows": rows, "changes": changes, "truncated": truncated}


# -- export -------------------------------------------------------------------


def export_tables(path: Path) -> tuple[str, dict[str, list[dict]]]:
    """App-defined schema as SQL and every app table's rows."""
    conn = connect(path)
    try:
        objects = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
            " AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
        ).fetchall()
        schema = []
        tables: dict[str, list[dict]] = {}
        for obj in objects:
            if obj["name"] in PLATFORM_OBJECTS:
                continue
            schema.append(obj["sql"] + ";")
            if obj["type"] == "table":
                quoted = '"' + obj["name"].replace('"', '""') + '"'
                cur = conn.execute(f"SELECT * FROM {quoted}")
                cols = [d[0] for d in cur.description]
                tables[obj["name"]] = [
                    {c: _cell(v) for c, v in zip(cols, r, strict=True)} for r in cur.fetchall()
                ]
    finally:
        conn.close()
    return "\n\n".join(schema) + ("\n" if schema else ""), tables
