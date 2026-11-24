#!/usr/bin/env python3
"""Pre-flight check for a simple-apps bundle, and optionally zip it.

    python3 check_bundle.py <app-dir> --name <app> [--zip out.zip]

Standard library only, so it runs anywhere (Claude Code, the claude.ai sandbox).
It mirrors the platform's deploy checks (references/ARCHITECTURE.md §4.2, §3.10):
bundle shape, app.json, migration/query files, and — in a scratch SQLite database
— applies the migrations and prepares every query under the same sandbox rules.
The platform re-checks everything at deploy; this only catches mistakes earlier.
Exit status 1 when there are errors; warnings never fail.
"""

import argparse
import json
import re
import sqlite3
import sys
import zipfile
from pathlib import Path

APP_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
RESERVED_APPS = {"system", "apps", "tmp", "health", "static"}
QUERY_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
MIGRATION_RE = re.compile(r"(\d{1,9})_[A-Za-z0-9_-]{1,64}\.sql")
NAME_LINE_RE = re.compile(r"--\s*name:\s*(\S+)\s*(\(write\))?\s*")
COMMENTS_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
ABS_ASSET_RE = re.compile(r"""(?:src|href)\s*=\s*["'](/(?!/)[^"']*)["']""")
EXCLUDED_PARTS = {"node_modules", "__MACOSX", "__pycache__"}
MAX_FILES = 2000
MAX_UNPACKED = 200 * 1024 * 1024
MAX_QUOTA_MB = 100
PLATFORM_SCHEMA = """
CREATE TABLE records (collection TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (collection, id));
CREATE INDEX records_by_time ON records (collection, created_at);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE _migrations (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, applied_at TEXT NOT NULL);
"""
PLATFORM_OBJECTS = {"records", "kv", "_migrations", "records_by_time"}

A = sqlite3
ALWAYS = {A.SQLITE_SELECT, A.SQLITE_READ, A.SQLITE_RECURSIVE}
WRITES = {A.SQLITE_INSERT, A.SQLITE_UPDATE, A.SQLITE_DELETE}
DDL = {
    A.SQLITE_CREATE_TABLE,
    A.SQLITE_CREATE_INDEX,
    A.SQLITE_CREATE_TRIGGER,
    A.SQLITE_CREATE_VIEW,
    A.SQLITE_DROP_TABLE,
    A.SQLITE_DROP_INDEX,
    A.SQLITE_DROP_TRIGGER,
    A.SQLITE_DROP_VIEW,
    A.SQLITE_ALTER_TABLE,
    A.SQLITE_REINDEX,
    A.SQLITE_ANALYZE,
}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def bundle_files(root: Path) -> list[Path]:
    """Files that go into the zip: no dotfiles, docs, node_modules or caches."""
    files = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(p.startswith(".") or p in EXCLUDED_PARTS for p in rel.parts):
            continue
        if path.is_file() and not path.is_symlink() and path.suffix.lower() != ".md":
            files.append(path)
    return files


# -- SQL, mirroring simple_apps/sqlstore.py ------------------------------------


def _is_blank(sql: str) -> bool:
    return not COMMENTS_RE.sub("", sql).replace(";", "").strip()


def split_statements(sql: str) -> list[str]:
    statements, start = [], 0
    for i, ch in enumerate(sql):
        if ch == ";" and sqlite3.complete_statement(sql[start : i + 1]):
            if not _is_blank(sql[start : i + 1]):
                statements.append(sql[start : i + 1].strip())
            start = i + 1
    if not _is_blank(sql[start:]):
        statements.append(sql[start:].strip())
    return statements


def parse_queries(text: str, report: Report) -> list[tuple[str, bool, list[str]]]:
    queries: list[tuple[str, bool, list[str]]] = []
    current: tuple[str, bool] | None = None
    body: list[str] = []

    def flush() -> None:
        if current is None:
            if not _is_blank("\n".join(body)):
                report.error("db/queries.sql: SQL before the first '-- name:' line")
            return
        stmts = split_statements("\n".join(body))
        if not stmts:
            report.error(f"db/queries.sql: query {current[0]!r} is empty")
        queries.append((current[0], current[1], stmts))

    for line in text.splitlines():
        m = NAME_LINE_RE.fullmatch(line.strip())
        if m is None:
            body.append(line)
            continue
        flush()
        name = m.group(1)
        if not QUERY_RE.fullmatch(name):
            report.error(f"db/queries.sql: invalid query name {name!r}")
        if any(q[0] == name for q in queries):
            report.error(f"db/queries.sql: duplicate query {name!r}")
        current, body = (name, m.group(2) is not None), []
    flush()
    for name, _write, stmts in queries:
        if any(re.search(r"(?<![:@$\w])\?\d*", COMMENTS_RE.sub("", s)) for s in stmts):
            report.warn(f"db/queries.sql: query {name!r} seems to use '?' — use named parameters (:x)")
    return queries


class Sandbox:
    def __init__(self, *, ddl: bool, write: bool):
        self.ddl, self.write, self.denied = ddl, write, None

    def _deny(self, reason: str) -> int:
        self.denied = self.denied or reason
        return sqlite3.SQLITE_DENY

    def __call__(self, action, arg1, arg2, _db, _src) -> int:
        if action in ALWAYS:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return (
                self._deny("load_extension is not allowed") if arg2 == "load_extension" else sqlite3.SQLITE_OK
            )
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
                return self._deny("platform objects (records, kv, _migrations) cannot be changed")
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA:
            return self._deny(f"PRAGMA {arg1} is not allowed")
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return self._deny("ATTACH / DETACH are not allowed")
        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
            return self._deny("transactions are managed by the platform")
        return self._deny("statement type is not allowed (temp objects and virtual tables are unsupported)")

    def explain(self, err: sqlite3.Error) -> str:
        return f"not allowed: {self.denied}" if self.denied and "not authorized" in str(err) else str(err)


class _AnyParams(dict):
    def __missing__(self, key):
        return None


def check_db(root: Path, report: Report) -> None:
    db_dir = root / "db"
    if not db_dir.exists():
        return
    migrations: list[tuple[int, str, str]] = []
    mig_dir = db_dir / "migrations"
    if mig_dir.is_dir():
        for path in sorted(mig_dir.iterdir()):
            m = MIGRATION_RE.fullmatch(path.name)
            if m is None or not path.is_file():
                report.error(f"db/migrations/{path.name}: expected <number>_<name>.sql")
                continue
            migrations.append((int(m.group(1)), path.name, path.read_text(encoding="utf-8")))
        migrations.sort()
        for a, b in zip(migrations, migrations[1:], strict=False):
            if a[0] == b[0]:
                report.error(f"db/migrations: {a[1]} and {b[1]} share number {a[0]}")
    qfile = db_dir / "queries.sql"
    queries = parse_queries(qfile.read_text(encoding="utf-8"), report) if qfile.is_file() else []
    if report.errors:
        return

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(PLATFORM_SCHEMA)
    try:
        for _n, name, text in migrations:
            box = Sandbox(ddl=True, write=True)
            for stmt in split_statements(text):
                conn.set_authorizer(box)
                try:
                    conn.execute(stmt).fetchall()
                except sqlite3.Error as err:
                    report.error(f"db/migrations/{name} failed: {box.explain(err)}")
                    return
                finally:
                    conn.set_authorizer(None)
        for name, write, stmts in queries:
            box = Sandbox(ddl=False, write=write)
            for stmt in stmts:
                conn.set_authorizer(box)
                try:
                    conn.execute("EXPLAIN " + stmt, _AnyParams()).fetchall()
                except sqlite3.Error as err:
                    report.error(f"db/queries.sql: query {name!r}: {box.explain(err)}")
                    break
                finally:
                    conn.set_authorizer(None)
    finally:
        conn.close()


# -- bundle -------------------------------------------------------------------


def check_manifest(root: Path, app: str, report: Report) -> None:
    path = root / "app.json"
    if not path.is_file():
        report.warn("no app.json: title defaults to the app name, quota to 10 MB")
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        report.error("app.json is not valid JSON")
        return
    if not isinstance(raw, dict):
        report.error("app.json must be a JSON object")
        return
    if "name" in raw and raw["name"] != app:
        report.error(f"app.json name {raw['name']!r} does not match {app!r}")
    if "sdk" in raw and raw["sdk"] != "v1":
        report.error('app.json sdk must be "v1"')
    title = raw.get("title", app)
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        report.error("app.json title must be a non-empty string of at most 100 chars")
    quota = raw.get("quota_mb", 10)
    if not isinstance(quota, int) or isinstance(quota, bool) or not 1 <= quota <= MAX_QUOTA_MB:
        report.error(f"app.json quota_mb must be an integer from 1 to {MAX_QUOTA_MB}")
    if not isinstance(raw.get("spa", False), bool):
        report.error("app.json spa must be true or false")


def check_sources(root: Path, files: list[Path], report: Report) -> None:
    uses_sdk = False
    loads_sdk = False
    for path in files:
        if path.suffix.lower() not in {".html", ".htm", ".js", ".mjs", ".css"}:
            continue
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        uses_sdk = uses_sdk or "simpleApps." in text
        loads_sdk = loads_sdk or "/_sdk/v1.js" in text
        for ref in ABS_ASSET_RE.findall(text):
            if not ref.startswith("/_sdk/"):
                report.error(
                    f"{rel}: absolute asset path {ref!r} — use a relative path (the app lives under /<app>/)"
                )
        if re.search(r"\.innerHTML\s*=", text):
            report.warn(f"{rel}: assigns innerHTML — build the DOM with createElement/textContent")
        if "localStorage" in text:
            report.warn(
                f"{rel}: uses localStorage — only for per-device UI prefs, keys prefixed with the app name"
            )
        for cdn in sorted(set(re.findall(r"https?://(?:cdn|unpkg|cdnjs)[^\s\"')]+", text)))[:3]:
            report.warn(f"{rel}: loads {cdn} from the internet — fine only if the user agreed")
    if uses_sdk and not loads_sdk:
        report.error('uses simpleApps but no page loads <script src="/_sdk/v1.js"></script>')


def check(root: Path, app: str) -> tuple[Report, list[Path]]:
    report = Report()
    if not APP_RE.fullmatch(app) or app in RESERVED_APPS:
        report.error(f"invalid app name {app!r}: ^[a-z0-9][a-z0-9-]{{0,39}}$, not {sorted(RESERVED_APPS)}")
    files = bundle_files(root)
    if not (root / "index.html").is_file():
        report.error("index.html must be at the root of the app folder (zip the contents, e.g. dist/)")
    if len(files) > MAX_FILES:
        report.error(f"more than {MAX_FILES} files")
    if sum(p.stat().st_size for p in files) > MAX_UNPACKED:
        report.error("bundle is larger than 200 MB unpacked")
    for path in root.rglob("*"):
        if path.is_symlink():
            report.error(f"symlinks are not allowed: {path.relative_to(root)}")
    check_manifest(root, app, report)
    check_sources(root, files, report)
    check_db(root, report)
    return report, files


def write_zip(root: Path, files: list[Path], out: Path) -> None:
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, path.relative_to(root).as_posix())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("app_dir", type=Path)
    parser.add_argument("--name", required=True, help="app name (the URL segment)")
    parser.add_argument("--zip", type=Path, help="write the bundle zip here when there are no errors")
    args = parser.parse_args(argv)
    root = args.app_dir.resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory")
        return 1
    report, files = check(root, args.name)
    for msg in report.warnings:
        print(f"warning: {msg}")
    for msg in report.errors:
        print(f"error: {msg}")
    if report.errors:
        print(f"FAILED: {len(report.errors)} error(s)")
        return 1
    if args.zip:
        write_zip(root, files, args.zip)
        print(f"OK: wrote {args.zip} ({len(files)} files)")
    else:
        print(f"OK: {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
