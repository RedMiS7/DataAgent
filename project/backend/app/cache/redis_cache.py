from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import redis

logger = logging.getLogger(__name__)

# After a failure, skip Redis for this long instead of waiting on every call.
RETRY_AFTER_SECONDS = 30.0
SOCKET_TIMEOUT_SECONDS = 0.2
CONNECT_TIMEOUT_SECONDS = 0.5


class RedisCache:
    """Redis behind the Cache protocol. Every error is logged and turned into a miss."""

    def __init__(self, client: redis.Redis) -> None:
        self.client = client
        self._down_until = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_url(cls, url: str) -> RedisCache:
        return cls(redis.Redis.from_url(url, socket_timeout=SOCKET_TIMEOUT_SECONDS,
                                        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS))

    def _call[T](self, action: str, key: str, call: Callable[[], T], fallback: T) -> T:
        if time.monotonic() < self._down_until:
            return fallback
        try:
            return call()
        except redis.RedisError as exc:
            with self._lock:
                self._down_until = time.monotonic() + RETRY_AFTER_SECONDS
            # The key's prefix only: values and full keys may carry query text.
            logger.warning("Redis %s失败，按未命中处理，%d 秒内不再尝试 key=%s error=%s",
                           action, int(RETRY_AFTER_SECONDS), key[:40], exc)
            return fallback

    def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        if not keys:
            return []
        misses: list[bytes | None] = [None] * len(keys)
        return self._call("读取", keys[0], lambda: list(self.client.mget(list(keys))), misses)

    def set_many(self, items: Mapping[str, bytes], ttl_seconds: int) -> None:
        if not items:
            return

        def write() -> None:
            pipe = self.client.pipeline(transaction=False)
            for key, value in items.items():
                pipe.set(key, value, ex=ttl_seconds)
            pipe.execute()

        self._call("写入", next(iter(items)), write, None)

    def incr(self, key: str, amount: int = 1, ttl_seconds: int | None = None) -> int | None:
        def count() -> int:
            value = int(self.client.incrby(key, amount))
            if ttl_seconds is not None and value == amount:
                self.client.expire(key, ttl_seconds)
            return value

        return self._call("计数", key, count, None)

    def clear(self, prefix: str) -> int:
        def delete() -> int:
            deleted = 0
            batch: list[Any] = []
            for key in self.client.scan_iter(match=f"{prefix}*", count=500):
                batch.append(key)
                if len(batch) >= 500:
                    deleted += int(self.client.delete(*batch))
                    batch = []
            if batch:
                deleted += int(self.client.delete(*batch))
            return deleted

        # Clearing is an admin command: let its failure show.
        return delete()

    def ping(self) -> bool:
        try:
            ok = bool(self.client.ping())
        except redis.RedisError:
            return False
        if ok:
            self._down_until = 0.0
        return ok
