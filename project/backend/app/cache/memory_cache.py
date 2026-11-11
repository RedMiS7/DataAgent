from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence


class MemoryCache:
    """An in-process cache for tests, and for rate limiting when there is no Redis."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[bytes, float | None]] = {}
        self._lock = threading.Lock()

    def _live(self, key: str) -> bytes | None:
        item = self._items.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and expires <= time.monotonic():
            del self._items[key]
            return None
        return value

    def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        with self._lock:
            return [self._live(key) for key in keys]

    def set_many(self, items: Mapping[str, bytes], ttl_seconds: int) -> None:
        expires = time.monotonic() + ttl_seconds
        with self._lock:
            for key, value in items.items():
                self._items[key] = (value, expires)

    def incr(self, key: str, amount: int = 1, ttl_seconds: int | None = None) -> int | None:
        with self._lock:
            current = self._live(key)
            count = (int(current) if current else 0) + amount
            expires = self._items[key][1] if current is not None else None
            if current is None and ttl_seconds is not None:
                expires = time.monotonic() + ttl_seconds
            self._items[key] = (str(count).encode(), expires)
            return count

    def clear(self, prefix: str) -> int:
        with self._lock:
            keys = [key for key in self._items if key.startswith(prefix)]
            for key in keys:
                del self._items[key]
            return len(keys)

    def ping(self) -> bool:
        return True
