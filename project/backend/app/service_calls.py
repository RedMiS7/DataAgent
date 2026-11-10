"""Retry transient provider errors within the active question's time budget."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .agent.trace import ErrorType, error_type

logger = logging.getLogger(__name__)

BACKOFF_SECONDS = (0.25, 0.5)
EMBEDDING_TIMEOUT_SECONDS = 15.0
RetryObserver = Callable[[str, ErrorType, int], None]


@dataclass(slots=True)
class ServiceBudget:
    started: float
    seconds: float
    on_retry: RetryObserver | None

    def remaining(self) -> float:
        return self.started + self.seconds - time.monotonic()


_budget: ContextVar[ServiceBudget | None] = ContextVar("service_budget", default=None)


@contextmanager
def service_budget(seconds: float, on_retry: RetryObserver | None = None) -> Iterator[ServiceBudget]:
    budget = ServiceBudget(time.monotonic(), seconds, on_retry)
    token = _budget.set(budget)
    try:
        yield budget
    finally:
        _budget.reset(token)


def remaining_timeout(limit: float) -> float:
    budget = _budget.get()
    seconds = min(limit, budget.remaining()) if budget else limit
    if seconds <= 0:
        raise TimeoutError("外部服务调用已用完时间预算")
    return seconds


def _retry_delay(service: str, exc: Exception, attempt: int) -> float | None:
    kind = error_type(exc)
    if attempt >= len(BACKOFF_SECONDS) or kind not in ("timeout", "429", "5xx"):
        return None
    delay = BACKOFF_SECONDS[attempt]
    budget = _budget.get()
    if budget is not None:
        if budget.remaining() <= delay:
            return None
        if budget.on_retry is not None:
            budget.on_retry(service, kind, attempt + 1)
    logger.info("%s 调用遇到 %s，%.2f 秒后重试（第 %d 次）", service, kind, delay, attempt + 1)
    return delay


async def retry_async[T](service: str, timeout: float,
                      call: Callable[[float], Awaitable[T]]) -> T:
    for attempt in range(len(BACKOFF_SECONDS) + 1):
        seconds = remaining_timeout(timeout)
        try:
            async with asyncio.timeout(seconds):
                return await call(seconds)
        except Exception as exc:
            delay = _retry_delay(service, exc, attempt)
            if delay is None:
                raise
            await asyncio.sleep(delay)
    raise AssertionError("retry loop did not return or raise")
