"""Tiny disk TTL cache for API responses. Keyed by endpoint+params; TTL per endpoint
(config.CACHE_TTL_S). Caching the fetch never fakes freshness — per-item staleness is
judged from each item's own lowTime/highTime inside the (possibly cached) response."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .config import DATA_DIR

CACHE_DIR = DATA_DIR / ".cache"


def _path(key: str):
    return CACHE_DIR / f"{hashlib.md5(key.encode()).hexdigest()[:16]}.json"


def get(key: str, ttl: float) -> Any | None:
    p = _path(key)
    if p.exists() and (time.time() - p.stat().st_mtime) < ttl:
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def set(key: str, value: Any) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _path(key).write_text(json.dumps(value))
