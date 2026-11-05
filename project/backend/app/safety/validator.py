from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp


class SQLValidationError(ValueError):
    pass


@dataclass(slots=True)
class SafeSQL:
    sql: str
    limit_applied: bool = False
    original_limit: int | None = None


ALLOWED_TABLES = {
    "categories", "channels", "products", "customers", "orders", "order_items",
    "refunds", "shipments", "ad_campaigns", "ad_daily_stats",
}

ALLOWED_SCHEMA = "safe"
BLOCKED_SCHEMAS = {"mysql", "information_schema", "performance_schema", "sys"}

BLOCKED_FUNCTIONS = {
    "sleep", "benchmark", "load_file", "get_lock", "release_lock", "master_pos_wait",
    "sys_exec", "sys_eval", "uuid_short",
}

BLOCKED_PATTERNS = [
    (re.compile(r"\binto\s+(out|dump)file\b", re.IGNORECASE), "禁止导出文件"),
    (re.compile(r"\bfor\s+update\b", re.IGNORECASE), "禁止加锁查询"),
    (re.compile(r"\block\s+in\s+share\s+mode\b", re.IGNORECASE), "禁止加锁查询"),
    (re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke)\b",
                re.IGNORECASE), "只允许查询语句"),
]


def _reject(reason: str) -> None:
    raise SQLValidationError(reason)


def _check_statement_type(statement: exp.Expression) -> None:
    if isinstance(statement, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Alter,
                              exp.Drop, exp.TruncateTable, exp.Merge, exp.Command)):
        _reject("只允许查询语句")
    if not isinstance(statement, (exp.Select, exp.Union)):
        _reject(f"不支持该语句类型：{type(statement).__name__}")


def _check_tables(statement: exp.Expression) -> None:
    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    for table in statement.find_all(exp.Table):
        schema = (table.db or "").lower()
        name = table.name.lower()
        if name in cte_names:
            continue
        if schema in BLOCKED_SCHEMAS:
            _reject(f"禁止访问系统库：{schema}")
        if schema and schema != ALLOWED_SCHEMA:
            _reject(f"禁止访问未授权的库：{schema}")
        if name not in ALLOWED_TABLES:
            _reject(f"禁止访问未授权的表：{name}")


def _check_functions(statement: exp.Expression, raw_sql: str) -> None:
    for func in statement.find_all(exp.Anonymous):
        if func.name.lower() in BLOCKED_FUNCTIONS:
            _reject(f"禁止调用危险函数：{func.name}")
    lowered = raw_sql.lower()
    for name in BLOCKED_FUNCTIONS:
        if re.search(rf"\b{name}\s*\(", lowered):
            _reject(f"禁止调用危险函数：{name}")


def _existing_limit(statement: exp.Expression) -> int | None:
    limit = statement.args.get("limit")
    if not isinstance(limit, exp.Limit):
        return None
    expression = limit.expression
    if isinstance(expression, exp.Literal) and expression.is_int:
        return int(expression.this)
    return None


def validate_and_limit(sql: str, max_rows: int) -> SafeSQL:
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        _reject("SQL 为空")

    for pattern, reason in BLOCKED_PATTERNS:
        if pattern.search(cleaned):
            _reject(reason)

    try:
        statements = sqlglot.parse(cleaned, read="mysql")
    except sqlglot.errors.ParseError as exc:
        raise SQLValidationError(f"SQL 无法解析：{exc}") from exc

    statements = [statement for statement in statements if statement is not None]
    if len(statements) != 1:
        _reject("只允许执行单条查询")

    statement = statements[0]
    _check_statement_type(statement)
    _check_tables(statement)
    _check_functions(statement, cleaned)

    original_limit = _existing_limit(statement)
    limit_applied = False
    if original_limit is None or original_limit > max_rows:
        statement = statement.limit(max_rows)
        limit_applied = True

    return SafeSQL(sql=statement.sql(dialect="mysql"), limit_applied=limit_applied,
                   original_limit=original_limit)