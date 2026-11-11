from __future__ import annotations

import time

from .base import KEY_PREFIX, Cache

WINDOW_SECONDS = 60


def limit_exceeded(cache: Cache, namespace: str, limit: int,
                   subjects: dict[str, str], now: float | None = None) -> int | None:
    """Count one request against each subject (session id, client IP) in a fixed
    one-minute window. The seconds to wait when any of them is over ``limit``, else
    None. A limit of 0, or a cache that cannot count, lets the request through."""
    if limit <= 0:
        return None
    moment = time.time() if now is None else now
    window = int(moment // WINDOW_SECONDS)
    over = False
    for kind, value in subjects.items():
        if not value:
            continue
        key = f"{KEY_PREFIX}rl:{namespace}:{kind}:{value}:{window}"
        count = cache.incr(key, 1, WINDOW_SECONDS)
        if count is not None and count > limit:
            over = True
    if not over:
        return None
    return max(1, WINDOW_SECONDS - int(moment % WINDOW_SECONDS))
