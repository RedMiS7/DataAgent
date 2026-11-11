from __future__ import annotations

import threading

from ..config import Settings
from .base import KEY_PREFIX, Cache, read_stats, record_lookup
from .memory_cache import MemoryCache
from .rate_limit import limit_exceeded
from .redis_cache import RedisCache

_shared: dict[str, RedisCache] = {}
_local_limits = MemoryCache()
_lock = threading.Lock()


def shared_redis(settings: Settings) -> RedisCache | None:
    """One client per Redis URL for the process; None when REDIS_URL is empty.

    The client is kept even while Redis is down: each call fails fast and counts as a
    miss, and Redis is used again once it is back.
    """
    url = settings.redis_url.strip()
    if not url:
        return None
    with _lock:
        if url not in _shared:
            _shared[url] = RedisCache.from_url(url)
        return _shared[url]


def caching(settings: Settings) -> Cache | None:
    """The cache for SQL results, vectors and rerank results; None when turned off."""
    return shared_redis(settings) if settings.cache_enabled else None


def rate_limit_store(settings: Settings) -> Cache:
    """Redis, so every process counts together; without it, this process's memory."""
    return shared_redis(settings) or _local_limits


__all__ = [
    "KEY_PREFIX",
    "Cache",
    "MemoryCache",
    "RedisCache",
    "caching",
    "limit_exceeded",
    "rate_limit_store",
    "read_stats",
    "record_lookup",
    "shared_redis",
]
