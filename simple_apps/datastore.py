"""Per-app data in apps/<app>/data.db: collections of JSON records plus a kv table
(ARCHITECTURE.md §3.3, §4.7).

Callers pass an already-validated app name; this module validates everything
else that comes from the request (collection, id, key, body shape).
"""

import json
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from . import names
from .db import connect, init_schema, transaction, utc_now
from .errors import ApiError

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    collection TEXT NOT NULL,
    id         TEXT NOT NULL,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (collection, id)
);
CREATE INDEX IF NOT EXISTS records_by_time ON records (collection, created_at);
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

ORDERS = {
    "created_at": "created_at ASC, id ASC",
    "-created_at": "created_at DESC, id DESC",
    "updated_at": "updated_at ASC, id ASC",
    "-updated_at": "updated_at DESC, id DESC",
}
MAX_LIMIT = 1000
DEFAULT_LIMIT = 100


def _record(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "data": json.loads(row["data"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _check_collection(collection: str) -> None:
    if not names.is_collection(collection):
        raise ApiError(400, "invalid collection name")


def _check_id(record_id: str) -> None:
    if not names.is_record_id(record_id):
        raise ApiError(400, "invalid record id")


def _check_key(key: str) -> None:
    if not names.is_kv_key(key):
        raise ApiError(400, "invalid key")


def _check_object(data: Any) -> None:
    if not isinstance(data, dict):
        raise ApiError(400, "record data must be a JSON object")


class AppData:
    """Data access for one app's data.db."""

    def __init__(self, path: Path, quota_mb: int):
        self.path = path
        self.quota_bytes = quota_mb * 1024 * 1024

    # -- quota -------------------------------------------------------------

    def used_bytes(self) -> int:
        conn = connect(self.path)
        try:
            pages = conn.execute("PRAGMA page_count").fetchone()[0]
            free = conn.execute("PRAGMA freelist_count").fetchone()[0]
            size = conn.execute("PRAGMA page_size").fetchone()[0]
        finally:
            conn.close()
        return (pages - free) * size

    def _check_quota(self) -> None:
        if self.used_bytes() >= self.quota_bytes:
            raise ApiError(507, "app data quota exceeded")

    # -- records -----------------------------------------------------------

    def list(
        self, collection: str, order: str = "created_at", limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> dict:
        _check_collection(collection)
        if order not in ORDERS:
            raise ApiError(400, f"order must be one of {', '.join(ORDERS)}")
        if not 1 <= limit <= MAX_LIMIT:
            raise ApiError(400, f"limit must be between 1 and {MAX_LIMIT}")
        if offset < 0:
            raise ApiError(400, "offset must be >= 0")
        conn = connect(self.path)
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM records WHERE collection = ?", (collection,)
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM records WHERE collection = ? ORDER BY {ORDERS[order]} LIMIT ? OFFSET ?",
                (collection, limit, offset),
            ).fetchall()
        finally:
            conn.close()
        return {"items": [_record(r) for r in rows], "total": total}

    def get(self, collection: str, record_id: str) -> dict:
        _check_collection(collection)
        _check_id(record_id)
        conn = connect(self.path)
        try:
            row = conn.execute(
                "SELECT * FROM records WHERE collection = ? AND id = ?", (collection, record_id)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ApiError(404, "record not found")
        return _record(row)

    def create(self, collection: str, data: Any) -> dict:
        _check_collection(collection)
        _check_object(data)
        self._check_quota()
        record_id = secrets.token_urlsafe(9)  # 12 chars
        now = utc_now()
        with transaction(self.path, immediate=True) as conn:
            conn.execute(
                "INSERT INTO records (collection, id, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (collection, record_id, _dump(data), now, now),
            )
        return {"id": record_id, "data": data, "created_at": now, "updated_at": now}

    def put(self, collection: str, record_id: str, data: Any) -> tuple[dict, bool]:
        """Create or replace. Returns (record, created)."""
        _check_collection(collection)
        _check_id(record_id)
        _check_object(data)
        self._check_quota()
        now = utc_now()
        with transaction(self.path, immediate=True) as conn:
            row = conn.execute(
                "SELECT created_at FROM records WHERE collection = ? AND id = ?",
                (collection, record_id),
            ).fetchone()
            if row:
                created_at = row["created_at"]
                conn.execute(
                    "UPDATE records SET data = ?, updated_at = ? WHERE collection = ? AND id = ?",
                    (_dump(data), now, collection, record_id),
                )
            else:
                created_at = now
                conn.execute(
                    "INSERT INTO records (collection, id, data, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (collection, record_id, _dump(data), now, now),
                )
        record = {"id": record_id, "data": data, "created_at": created_at, "updated_at": now}
        return record, row is None

    def patch(self, collection: str, record_id: str, patch: Any) -> dict:
        """Shallow-merge `patch` into the record; a null value removes the field."""
        _check_collection(collection)
        _check_id(record_id)
        _check_object(patch)
        self._check_quota()
        now = utc_now()
        with transaction(self.path, immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM records WHERE collection = ? AND id = ?", (collection, record_id)
            ).fetchone()
            if row is None:
                raise ApiError(404, "record not found")
            data = json.loads(row["data"])
            for field, value in patch.items():
                if value is None:
                    data.pop(field, None)
                else:
                    data[field] = value
            conn.execute(
                "UPDATE records SET data = ?, updated_at = ? WHERE collection = ? AND id = ?",
                (_dump(data), now, collection, record_id),
            )
        return {"id": record_id, "data": data, "created_at": row["created_at"], "updated_at": now}

    def delete(self, collection: str, record_id: str) -> None:
        _check_collection(collection)
        _check_id(record_id)
        with transaction(self.path, immediate=True) as conn:
            cur = conn.execute("DELETE FROM records WHERE collection = ? AND id = ?", (collection, record_id))
        if cur.rowcount == 0:
            raise ApiError(404, "record not found")

    # -- kv ----------------------------------------------------------------

    def kv_get(self, key: str) -> dict:
        _check_key(key)
        conn = connect(self.path)
        try:
            row = conn.execute("SELECT * FROM kv WHERE key = ?", (key,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ApiError(404, "key not found")
        return {"key": key, "value": json.loads(row["value"]), "updated_at": row["updated_at"]}

    def kv_set(self, key: str, value: Any) -> dict:
        _check_key(key)
        self._check_quota()
        now = utc_now()
        with transaction(self.path, immediate=True) as conn:
            conn.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (key, _dump(value), now),
            )
        return {"key": key, "value": value, "updated_at": now}

    def kv_delete(self, key: str) -> None:
        _check_key(key)
        with transaction(self.path, immediate=True) as conn:
            cur = conn.execute("DELETE FROM kv WHERE key = ?", (key,))
        if cur.rowcount == 0:
            raise ApiError(404, "key not found")

    # -- export ------------------------------------------------------------

    def export(self) -> tuple[dict, dict]:
        """All data as ({collection: [record, ...]}, {key: value})."""
        conn = connect(self.path)
        try:
            rows = conn.execute("SELECT * FROM records ORDER BY collection, created_at, id").fetchall()
            kv_rows = conn.execute("SELECT key, value FROM kv ORDER BY key").fetchall()
        finally:
            conn.close()
        records: dict[str, list] = {}
        for row in rows:
            records.setdefault(row["collection"], []).append(_record(row))
        kv = {row["key"]: json.loads(row["value"]) for row in kv_rows}
        return records, kv


def data_path(apps_dir: Path, app: str) -> Path:
    return apps_dir / app / "data.db"


def init_app_data(apps_dir: Path, app: str) -> None:
    init_schema(data_path(apps_dir, app), SCHEMA)
