"""Name validation (ARCHITECTURE.md §3.4).

Every name that ends up in a filesystem path or SQL value goes through here first.
"""

import re

APP_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
COLLECTION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
RECORD_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
KV_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")

RESERVED_APPS = frozenset({"system", "apps", "tmp", "health", "static"})


def is_app_name(name: str) -> bool:
    return bool(APP_RE.fullmatch(name)) and name not in RESERVED_APPS


def is_collection(name: str) -> bool:
    return bool(COLLECTION_RE.fullmatch(name))


def is_record_id(name: str) -> bool:
    return bool(RECORD_ID_RE.fullmatch(name))


def is_kv_key(name: str) -> bool:
    return bool(KV_KEY_RE.fullmatch(name)) and name not in {".", ".."}
