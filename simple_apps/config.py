"""Runtime configuration, read once from environment variables (ARCHITECTURE.md §3.8)."""

import os
from dataclasses import dataclass
from pathlib import Path

MB = 1024 * 1024


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    admin_token: str
    data_dir: Path = Path("/data")
    port: int = 8080
    max_bundle_bytes: int = 50 * MB
    max_unpacked_bytes: int = 200 * MB
    max_files: int = 2000
    max_body_bytes: int = 1024 * 1024
    default_quota_mb: int = 10
    max_quota_mb: int = 100
    keep_versions: int = 3
    max_threads: int = 32
    request_timeout: float = 30.0

    @property
    def system_dir(self) -> Path:
        return self.data_dir / "system"

    @property
    def apps_dir(self) -> Path:
        return self.data_dir / "apps"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @classmethod
    def from_env(cls, env: "os._Environ[str] | dict[str, str]" = os.environ) -> "Config":
        token = env.get("ADMIN_TOKEN", "")
        if len(token) < 24:
            raise ConfigError("ADMIN_TOKEN must be set and at least 24 characters long")

        def num(name: str, default: int) -> int:
            raw = env.get(name)
            if raw is None or raw == "":
                return default
            try:
                value = int(raw)
            except ValueError:
                raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
            if value <= 0:
                raise ConfigError(f"{name} must be positive, got {value}")
            return value

        return cls(
            admin_token=token,
            data_dir=Path(env.get("DATA_DIR") or "/data"),
            port=num("PORT", 8080),
            max_bundle_bytes=num("MAX_BUNDLE_MB", 50) * MB,
            max_unpacked_bytes=num("MAX_UNPACKED_MB", 200) * MB,
            max_files=num("MAX_FILES", 2000),
            max_body_bytes=num("MAX_BODY_KB", 1024) * 1024,
            default_quota_mb=num("DEFAULT_QUOTA_MB", 10),
            max_quota_mb=num("MAX_QUOTA_MB", 100),
            keep_versions=num("KEEP_VERSIONS", 3),
            max_threads=num("MAX_THREADS", 32),
            request_timeout=float(num("REQUEST_TIMEOUT", 30)),
        )
