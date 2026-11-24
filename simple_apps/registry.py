"""Platform state in system/system.db (ARCHITECTURE.md §3.3).

No data endpoint ever opens this file; only the admin API and static serving read it.
"""

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .db import connect, init_schema, transaction, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    name            TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    quota_mb        INTEGER NOT NULL,
    spa             INTEGER NOT NULL,
    current_version INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deployments (
    app         TEXT NOT NULL REFERENCES apps(name) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    deployed_at TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    file_count  INTEGER NOT NULL,
    PRIMARY KEY (app, version)
);
"""


@dataclass(frozen=True)
class AppInfo:
    name: str
    title: str
    quota_mb: int
    spa: bool
    current_version: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Deployment:
    version: int
    deployed_at: str
    size_bytes: int
    file_count: int

    def to_dict(self) -> dict:
        return asdict(self)


def _app(row: sqlite3.Row) -> AppInfo:
    return AppInfo(
        name=row["name"],
        title=row["title"],
        quota_mb=row["quota_mb"],
        spa=bool(row["spa"]),
        current_version=row["current_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class Registry:
    def __init__(self, system_dir: Path):
        self.path = system_dir / "system.db"
        init_schema(self.path, SCHEMA)

    def get(self, name: str) -> AppInfo | None:
        conn = connect(self.path)
        try:
            row = conn.execute("SELECT * FROM apps WHERE name = ?", (name,)).fetchone()
        finally:
            conn.close()
        return _app(row) if row else None

    def list(self) -> list[AppInfo]:
        conn = connect(self.path)
        try:
            rows = conn.execute("SELECT * FROM apps ORDER BY name").fetchall()
        finally:
            conn.close()
        return [_app(r) for r in rows]

    def deployments(self, name: str) -> list[Deployment]:
        conn = connect(self.path)
        try:
            rows = conn.execute(
                "SELECT version, deployed_at, size_bytes, file_count FROM deployments"
                " WHERE app = ? ORDER BY version DESC",
                (name,),
            ).fetchall()
        finally:
            conn.close()
        return [Deployment(**dict(r)) for r in rows]

    def max_version(self, name: str) -> int:
        conn = connect(self.path)
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM deployments WHERE app = ?", (name,)
            ).fetchone()
        finally:
            conn.close()
        return int(row[0])

    def record_deploy(
        self,
        name: str,
        title: str,
        quota_mb: int,
        spa: bool,
        version: int,
        size_bytes: int,
        file_count: int,
    ) -> bool:
        """Upsert the app and add a deployment row. Returns True if the app is new."""
        now = utc_now()
        with transaction(self.path, immediate=True) as conn:
            exists = conn.execute("SELECT 1 FROM apps WHERE name = ?", (name,)).fetchone()
            if exists:
                conn.execute(
                    "UPDATE apps SET title = ?, quota_mb = ?, spa = ?, current_version = ?,"
                    " updated_at = ? WHERE name = ?",
                    (title, quota_mb, int(spa), version, now, name),
                )
            else:
                conn.execute(
                    "INSERT INTO apps (name, title, quota_mb, spa, current_version,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (name, title, quota_mb, int(spa), version, now, now),
                )
            conn.execute(
                "INSERT INTO deployments (app, version, deployed_at, size_bytes, file_count)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, version, now, size_bytes, file_count),
            )
        return not exists

    def set_current(self, name: str, version: int) -> None:
        with transaction(self.path, immediate=True) as conn:
            conn.execute(
                "UPDATE apps SET current_version = ?, updated_at = ? WHERE name = ?",
                (version, utc_now(), name),
            )

    def forget_versions(self, name: str, versions: list[int]) -> None:
        if not versions:
            return
        with transaction(self.path, immediate=True) as conn:
            conn.executemany(
                "DELETE FROM deployments WHERE app = ? AND version = ?",
                [(name, v) for v in versions],
            )

    def delete(self, name: str) -> None:
        with transaction(self.path, immediate=True) as conn:
            conn.execute("DELETE FROM apps WHERE name = ?", (name,))
