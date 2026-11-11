from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

# Every key starts with this; bump the version when a stored value changes shape.
KEY_PREFIX = "da:v1:"
STATS_KINDS = ("sql", "emb", "rerank")


@runtime_checkable
class Cache(Protocol):
    """A key-value cache. Failures never raise: a read that fails is a miss, a write
    that fails is skipped, so a slow or missing cache cannot break a question."""

    def get_many(self, keys: Sequence[str]) -> list[bytes | None]: ...

    def set_many(self, items: Mapping[str, bytes], ttl_seconds: int) -> None: ...

    def incr(self, key: str, amount: int = 1, ttl_seconds: int | None = None) -> int | None:
        """The new count, or None when the cache could not be reached."""
        ...

    def clear(self, prefix: str) -> int:
        """Delete every key starting with ``prefix``; the number deleted."""
        ...

    def ping(self) -> bool: ...


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def jittered(ttl_seconds: int) -> int:
    """The TTL plus up to 10%, so keys written together do not all expire together."""
    return ttl_seconds + random.randint(0, ttl_seconds // 10)


def stats_key(namespace: str, kind: str, outcome: str) -> str:
    return f"{KEY_PREFIX}stats:{namespace}:{kind}:{outcome}"


def record_lookup(cache: Cache, namespace: str, kind: str, hits: int, misses: int) -> None:
    if hits:
        cache.incr(stats_key(namespace, kind, "hit"), hits)
    if misses:
        cache.incr(stats_key(namespace, kind, "miss"), misses)


def read_stats(cache: Cache, namespace: str) -> dict[str, dict[str, int]]:
    keys = [stats_key(namespace, kind, outcome)
            for kind in STATS_KINDS for outcome in ("hit", "miss")]
    values = cache.get_many(keys)
    counts = [int(value) if value else 0 for value in values]
    return {kind: {"hit": counts[2 * index], "miss": counts[2 * index + 1]}
            for index, kind in enumerate(STATS_KINDS)}
