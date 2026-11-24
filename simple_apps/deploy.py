"""Bundle validation, extraction, atomic version switching, rollback, delete and export
(ARCHITECTURE.md §4.2–§4.6).
"""

import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from . import datastore
from .config import Config
from .errors import ApiError
from .registry import AppInfo, Registry

log = logging.getLogger(__name__)

CHUNK = 64 * 1024
MANIFEST_MAX_BYTES = 64 * 1024
VERSION_DIR_RE = re.compile(r"v(\d+)")
IGNORED_PARTS = {"__MACOSX"}
IGNORED_FILES = {".DS_Store", "Thumbs.db"}


@dataclass(frozen=True)
class Manifest:
    title: str
    quota_mb: int
    spa: bool


@dataclass(frozen=True)
class Member:
    info: zipfile.ZipInfo
    path: PurePosixPath  # destination, relative to the site root


class Deployer:
    def __init__(self, config: Config, registry: Registry):
        self.config = config
        self.registry = registry
        self._lock = threading.Lock()
        config.apps_dir.mkdir(parents=True, exist_ok=True)
        config.tmp_dir.mkdir(parents=True, exist_ok=True)

    # -- paths -------------------------------------------------------------

    def site_dir(self, app: str) -> Path:
        return self.config.apps_dir / app / "site"

    def _disk_versions(self, app: str) -> list[int]:
        site = self.site_dir(app)
        if not site.is_dir():
            return []
        return sorted(
            int(m.group(1))
            for p in site.iterdir()
            if (m := VERSION_DIR_RE.fullmatch(p.name)) and p.is_dir() and not p.is_symlink()
        )

    def cleanup_tmp(self) -> None:
        """Remove staging leftovers from an interrupted deploy (called at start-up)."""
        for entry in self.config.tmp_dir.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)

    def _tmp_path(self, suffix: str = "") -> Path:
        return self.config.tmp_dir / f"{secrets.token_hex(8)}{suffix}"

    # -- deploy ------------------------------------------------------------

    def deploy(self, app: str, body: BinaryIO, length: int) -> tuple[AppInfo, bool]:
        """Stage, validate, extract and switch to a new version. Returns (app, created)."""
        if length > self.config.max_bundle_bytes:
            raise ApiError(413, f"bundle larger than {self.config.max_bundle_bytes // (1024 * 1024)} MB")
        zip_path = self._tmp_path(".zip")
        staging = self._tmp_path()
        try:
            self._stage(body, length, zip_path)
            try:
                zf = zipfile.ZipFile(zip_path)
            except zipfile.BadZipFile:
                raise ApiError(400, "body is not a valid zip file") from None
            with zf:
                members = self._inspect(zf)
                manifest = self._manifest(zf, members, app)
                total = self._extract(zf, members, staging)
            return self._commit(app, staging, manifest, total, len(members))
        finally:
            zip_path.unlink(missing_ok=True)
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _stage(self, body: BinaryIO, length: int, dest: Path) -> None:
        remaining = length
        with open(dest, "wb") as out:
            while remaining > 0:
                chunk = body.read(min(CHUNK, remaining))
                if not chunk:
                    raise ApiError(400, "request body ended early")
                out.write(chunk)
                remaining -= len(chunk)

    def _inspect(self, zf: zipfile.ZipFile) -> list[Member]:
        members: list[Member] = []
        seen: set[str] = set()
        declared = 0
        for info in zf.infolist():
            name = info.filename
            if "\\" in name or "\x00" in name:
                raise ApiError(400, f"invalid path in zip: {name!r}")
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0]):
                raise ApiError(400, f"unsafe path in zip: {name!r}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ApiError(400, f"symlinks are not allowed: {name!r}")
            if info.is_dir():
                continue
            parts = [p for p in path.parts if p not in ("", ".")]
            if not parts or IGNORED_PARTS.intersection(parts) or parts[-1] in IGNORED_FILES:
                continue
            clean = PurePosixPath(*parts)
            if str(clean) in seen:
                raise ApiError(400, f"duplicate path in zip: {clean}")
            seen.add(str(clean))
            declared += info.file_size
            members.append(Member(info, clean))

        if len(members) > self.config.max_files:
            raise ApiError(400, f"bundle has more than {self.config.max_files} files")
        if declared > self.config.max_unpacked_bytes:
            raise ApiError(400, "bundle unpacks to more than the allowed size")

        # Zipping a dist/ folder instead of its contents is the common mistake: strip it.
        roots = {m.path.parts[0] for m in members}
        if len(roots) == 1 and all(len(m.path.parts) > 1 for m in members):
            members = [Member(m.info, PurePosixPath(*m.path.parts[1:])) for m in members]

        if not any(m.path == PurePosixPath("index.html") for m in members):
            raise ApiError(400, "bundle must contain index.html at its root")
        return members

    def _manifest(self, zf: zipfile.ZipFile, members: list[Member], app: str) -> Manifest:
        cfg = self.config
        entry = next((m for m in members if m.path == PurePosixPath("app.json")), None)
        if entry is None:
            return Manifest(title=app, quota_mb=cfg.default_quota_mb, spa=False)
        if entry.info.file_size > MANIFEST_MAX_BYTES:
            raise ApiError(400, "app.json is too large")
        try:
            raw = json.loads(zf.read(entry.info))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "app.json is not valid JSON") from None
        if not isinstance(raw, dict):
            raise ApiError(400, "app.json must be a JSON object")

        if "name" in raw and raw["name"] != app:
            raise ApiError(400, f"app.json name {raw['name']!r} does not match {app!r}")
        if "sdk" in raw and raw["sdk"] != "v1":
            raise ApiError(400, 'app.json sdk must be "v1"')
        title = raw.get("title", app)
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            raise ApiError(400, "app.json title must be a non-empty string of at most 100 chars")
        quota = raw.get("quota_mb", cfg.default_quota_mb)
        if not isinstance(quota, int) or isinstance(quota, bool) or not 1 <= quota <= cfg.max_quota_mb:
            raise ApiError(400, f"app.json quota_mb must be an integer from 1 to {cfg.max_quota_mb}")
        spa = raw.get("spa", False)
        if not isinstance(spa, bool):
            raise ApiError(400, "app.json spa must be true or false")
        return Manifest(title=title.strip(), quota_mb=quota, spa=spa)

    def _extract(self, zf: zipfile.ZipFile, members: list[Member], staging: Path) -> int:
        """Copy entries out, counting real bytes so a lying header cannot exceed limits."""
        limit = self.config.max_unpacked_bytes
        total = 0
        staging.mkdir()
        for member in members:
            dest = staging.joinpath(*member.path.parts)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member.info) as src, open(dest, "xb") as out:
                    while chunk := src.read(CHUNK):
                        total += len(chunk)
                        if total > limit:
                            raise ApiError(400, "bundle unpacks to more than the allowed size")
                        out.write(chunk)
            except (FileExistsError, NotADirectoryError, IsADirectoryError):
                raise ApiError(400, f"conflicting paths in zip near {member.path}") from None
            except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
                raise ApiError(400, f"corrupt zip entry {member.path}: {exc}") from None
        return total

    def _commit(
        self, app: str, staging: Path, manifest: Manifest, size: int, files: int
    ) -> tuple[AppInfo, bool]:
        with self._lock:
            site = self.site_dir(app)
            site.mkdir(parents=True, exist_ok=True)
            version = max([self.registry.max_version(app), *self._disk_versions(app)]) + 1
            staging.rename(site / f"v{version}")
            self._point_current(app, version)
            created = self.registry.record_deploy(
                app, manifest.title, manifest.quota_mb, manifest.spa, version, size, files
            )
            datastore.init_app_data(self.config.apps_dir, app)
            self._prune(app, version)
            info = self.registry.get(app)
            assert info is not None
            log.info("deployed %s v%d (%d files, %d bytes)", app, version, files, size)
            return info, created

    def _point_current(self, app: str, version: int) -> None:
        """Atomically repoint site/current at v<version>."""
        site = self.site_dir(app)
        tmp_link = site / "current.tmp"
        tmp_link.unlink(missing_ok=True)
        os.symlink(f"v{version}", tmp_link)
        os.replace(tmp_link, site / "current")

    def _prune(self, app: str, current: int) -> None:
        keep_n = self.config.keep_versions
        known = [d.version for d in self.registry.deployments(app)]  # newest first
        keep = set(known[:keep_n]) | {current}
        self.registry.forget_versions(app, [v for v in known if v not in keep])
        for v in self._disk_versions(app):
            if v not in keep:
                shutil.rmtree(self.site_dir(app) / f"v{v}", ignore_errors=True)

    # -- rollback / delete -------------------------------------------------

    def rollback(self, app: str) -> AppInfo:
        with self._lock:
            info = self.registry.get(app)
            if info is None:
                raise ApiError(404, "app not found")
            on_disk = set(self._disk_versions(app))
            older = [
                d.version
                for d in self.registry.deployments(app)
                if d.version < info.current_version and d.version in on_disk
            ]
            if not older:
                raise ApiError(409, "no older version to roll back to")
            target = max(older)
            self._point_current(app, target)
            self.registry.set_current(app, target)
            log.info("rolled back %s v%d -> v%d", app, info.current_version, target)
            result = self.registry.get(app)
            assert result is not None
            return result

    def delete(self, app: str) -> None:
        with self._lock:
            if self.registry.get(app) is None:
                raise ApiError(404, "app not found")
            self.registry.delete(app)
            shutil.rmtree(self.config.apps_dir / app, ignore_errors=True)
            log.info("deleted %s", app)

    # -- static files ------------------------------------------------------

    def resolve_static(self, info: AppInfo, rel: str) -> Path | None:
        """Map a URL path (already percent-decoded, relative to /<app>/) to a file,
        or None. Never returns a path outside the current version."""
        if "\x00" in rel or "\\" in rel:
            return None
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if ".." in parts:
            return None
        root = (self.site_dir(info.name) / "current").resolve()
        candidate = root.joinpath(*parts).resolve()
        if not candidate.is_relative_to(root):
            return None
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if candidate.is_file():
            return candidate
        if info.spa and (not parts or "." not in parts[-1]):
            index = root / "index.html"
            return index if index.is_file() else None
        return None

    # -- export ------------------------------------------------------------

    def export(self, app: str) -> Path:
        """Write site + data to a temporary zip and return its path (caller deletes it)."""
        info = self.registry.get(app)
        if info is None:
            raise ApiError(404, "app not found")
        store = datastore.AppData(datastore.data_path(self.config.apps_dir, app), info.quota_mb)
        records, kv = store.export()
        meta = info.to_dict() | {"deployments": [d.to_dict() for d in self.registry.deployments(app)]}
        out = self._tmp_path(".zip")
        root = (self.site_dir(app) / "current").resolve()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    zf.write(path, f"site/{path.relative_to(root).as_posix()}")
            zf.writestr("data/records.json", json.dumps(records, ensure_ascii=False, indent=2))
            zf.writestr("data/kv.json", json.dumps(kv, ensure_ascii=False, indent=2))
            zf.writestr("app.json", json.dumps(meta, ensure_ascii=False, indent=2))
        return out
