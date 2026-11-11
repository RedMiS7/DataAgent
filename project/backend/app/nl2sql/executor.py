from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..cache import caching, record_lookup
from ..cache.base import KEY_PREFIX, digest, jittered
from ..config import Settings
from ..db.engine import business_connection
from ..safety.validator import SafeSQL

logger = logging.getLogger(__name__)

# Results larger than this, encoded, are not cached.
MAX_CACHED_BYTES = 1024 * 1024


class SQLExecutionError(RuntimeError):
    def __init__(self, message: str, sql: str) -> None:
        super().__init__(message)
        self.sql = sql


@dataclass(slots=True)
class ExecutionResult:
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False
    duration_ms: int = 0
    sql: str = ""
    # Read from the cache instead of the database.
    cached: bool = False


def normalize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def sql_cache_key(settings: Settings, sql: str) -> str:
    """Keyed by everything that changes the result: the database, the data date, the
    row limit and the statement with its whitespace collapsed."""
    statement = " ".join(sql.split())
    return (f"{KEY_PREFIX}sql:{settings.mysql_business_db}:{settings.as_of_date.isoformat()}:"
            f"{settings.sql_max_rows}:{digest(statement)}")


def _encode(result: ExecutionResult) -> bytes | None:
    try:
        data = json.dumps({"columns": result.columns, "rows": result.rows,
                           "truncated": result.truncated}, ensure_ascii=False)
    except (TypeError, ValueError):
        # A value JSON cannot hold as it is; caching it would change its type.
        return None
    encoded = data.encode("utf-8")
    return encoded if len(encoded) <= MAX_CACHED_BYTES else None


def _decode(value: bytes, sql: str, duration_ms: int) -> ExecutionResult | None:
    try:
        data = json.loads(value)
        return ExecutionResult(columns=list(data["columns"]), rows=list(data["rows"]),
                               truncated=bool(data["truncated"]), duration_ms=duration_ms,
                               sql=sql, cached=True)
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("SQL 缓存内容无法解析，改为查库 error=%s", exc)
        return None


def execute_sql(settings: Settings, safe_sql: SafeSQL) -> ExecutionResult:
    """Run a statement that already passed the safety checks, from the result cache
    when the same statement ran before.

    The cache holds results only, keyed by the checked statement, so it never lets a
    statement skip the checks.
    """
    started = time.perf_counter()
    cache = caching(settings)
    key = sql_cache_key(settings, safe_sql.sql) if cache is not None else ""
    if cache is not None:
        stored = cache.get_many([key])[0]
        hit = _decode(stored, safe_sql.sql, 0) if stored is not None else None
        record_lookup(cache, settings.mysql_business_db, "sql", int(hit is not None),
                      int(hit is None))
        if hit is not None:
            hit.duration_ms = int((time.perf_counter() - started) * 1000)
            return hit

    try:
        with business_connection(settings) as conn:
            result = conn.execute(text(safe_sql.sql))
            columns = list(result.keys())
            fetched = result.fetchmany(settings.sql_max_rows + 1)
    except SQLAlchemyError as exc:
        raise SQLExecutionError(f"SQL 执行失败：{exc}", safe_sql.sql) from exc

    truncated = len(fetched) > settings.sql_max_rows
    rows = [[normalize_value(value) for value in row] for row in fetched[:settings.sql_max_rows]]
    duration_ms = int((time.perf_counter() - started) * 1000)
    execution = ExecutionResult(columns=columns, rows=rows, truncated=truncated,
                                duration_ms=duration_ms, sql=safe_sql.sql)
    if cache is not None:
        encoded = _encode(execution)
        if encoded is not None:
            cache.set_many({key: encoded}, jittered(settings.sql_cache_ttl_seconds))
    return execution
