from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..config import Settings
from ..db.engine import business_connection
from ..safety.validator import SafeSQL


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


def normalize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def execute_sql(settings: Settings, safe_sql: SafeSQL) -> ExecutionResult:
    started = time.perf_counter()
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
    return ExecutionResult(columns=columns, rows=rows, truncated=truncated,
                           duration_ms=duration_ms, sql=safe_sql.sql)