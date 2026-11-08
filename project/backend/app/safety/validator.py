from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.qualify import qualify

# Table name -> its column names, as the database reports them.
Columns = Mapping[str, Sequence[str]]

UNKNOWN_COLUMN = re.compile(r"Unknown column: (\w+)")
UNRESOLVED_COLUMN = re.compile(r"Column '(\w+)' could not be resolved(?: for table: '(\w+)')?")
MAX_SUGGESTIONS = 3
MAX_LISTED_TABLES = 3


class SQLValidationError(ValueError):
    pass


@dataclass(slots=True)
class SafeSQL:
    sql: str
    limit_applied: bool = False
    original_limit: int | None = None


@dataclass(frozen=True, slots=True)
class SQLPolicy:
    """What a query may read, from the settings and the database itself.

    ``columns`` are the tables of ``schema`` that the read-only account can see, with
    their columns: only these tables are allowed, and every column must exist. None
    when they could not be read; then only the schema is checked, and the account's
    grants stay the last guard.
    """

    schema: str
    dialect: str
    columns: Columns | None = None


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


def _check_tables(statement: exp.Expression, policy: SQLPolicy) -> None:
    allowed = ({table.lower() for table in policy.columns}
               if policy.columns is not None else None)
    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    for table in statement.find_all(exp.Table):
        schema = (table.db or "").lower()
        name = table.name.lower()
        if name in cte_names:
            continue
        if schema in BLOCKED_SCHEMAS:
            _reject(f"禁止访问系统库：{schema}")
        if schema and schema != policy.schema.lower():
            _reject(f"禁止访问未授权的库：{schema}")
        if allowed is not None and name not in allowed:
            _reject(f"禁止访问未授权的表：{name}")


def _check_functions(statement: exp.Expression, raw_sql: str) -> None:
    for func in statement.find_all(exp.Anonymous):
        if func.name.lower() in BLOCKED_FUNCTIONS:
            _reject(f"禁止调用危险函数：{func.name}")
    lowered = raw_sql.lower()
    for name in BLOCKED_FUNCTIONS:
        if re.search(rf"\b{name}\s*\(", lowered):
            _reject(f"禁止调用危险函数：{name}")


def _lowered(statement: exp.Expression) -> exp.Expression:
    """A copy with every identifier in lower case: MySQL column names ignore case."""
    copy = statement.copy()
    for identifier in copy.find_all(exp.Identifier):
        identifier.set("this", identifier.this.lower())
    return copy


def _alias_tables(statement: exp.Expression) -> dict[str, str]:
    """Alias (or name) -> table name, for the physical tables a query reads."""
    tables: dict[str, str] = {}
    cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
    for table in statement.find_all(exp.Table):
        if table.name in cte_names:
            continue
        tables[table.alias_or_name] = table.name
        tables.setdefault(table.name, table.name)
    return tables


def _suggestions(name: str, tables: list[str], columns: Columns) -> list[str]:
    """Columns that look like ``name``: containing it first, then by spelling."""
    found: list[str] = []
    candidates = [(table, column) for table in tables for column in columns.get(table, ())]
    for table, column in candidates:
        if len(name) >= 3 and (name in column or column in name):
            found.append(f"{table}.{column}")
    spelled = difflib.get_close_matches(name, [column for _, column in candidates],
                                        n=MAX_SUGGESTIONS, cutoff=0.5)
    for table, column in candidates:
        if column in spelled and f"{table}.{column}" not in found:
            found.append(f"{table}.{column}")
    return found[:MAX_SUGGESTIONS]


def unknown_column_hint(sql: str, column: str, columns: Columns, dialect: str) -> str:
    """What to write instead of ``column`` ("o.cost" or "cost"): close matches and the
    real columns of the tables it may belong to."""
    try:
        statement = _lowered(sqlglot.parse_one(sql, read=dialect))
    except sqlglot.errors.ParseError:
        return ""
    aliases = _alias_tables(statement)
    qualifier, _, name = column.lower().rpartition(".")
    unknown_alias = bool(qualifier) and qualifier not in aliases
    if qualifier and not unknown_alias:
        tables = [aliases[qualifier]]
    else:
        tables = list(dict.fromkeys(aliases.values()))
    tables = [table for table in tables if table in columns]
    if not tables:
        return ""
    if unknown_alias:
        known = "、".join(f"{alias}（{table}）" for alias, table in aliases.items() if alias != table)
        lines = [f"字段不存在：{column}，查询里没有叫 {qualifier} 的表或别名。"
                 + (f"可用的别名：{known}。" if known else "")]
    else:
        label = f"{tables[0]}.{name}" if len(tables) == 1 else name
        lines = [f"字段不存在：{label}。"]
    close = _suggestions(name, tables, columns)
    if close:
        lines[0] += f"最接近的字段：{'、'.join(close)}。"
    for table in tables[:MAX_LISTED_TABLES]:
        lines.append(f"{table} 的字段：{', '.join(columns[table])}")
    return "\n".join(lines)


def _check_columns(statement: exp.Expression, sql: str, policy: SQLPolicy,
                   columns: Columns) -> None:
    """Reject a column the tables do not have, saying what to write instead.

    Anything else qualify cannot handle is let through: MySQL has the last word,
    and a valid query must never be blocked here.
    """
    lowered = _lowered(statement)
    schema = {policy.schema: {table: {column.lower(): "TEXT" for column in names}
                              for table, names in columns.items()}}
    try:
        qualify(lowered, schema=schema, dialect=policy.dialect, validate_qualify_columns=True,
                quote_identifiers=False, identify=False)
    except OptimizeError as exc:
        message = str(exc)
        unknown = UNKNOWN_COLUMN.search(message)
        unresolved = UNRESOLVED_COLUMN.search(message)
        if unknown:
            name = unknown.group(1)
            column = next((f"{node.table}.{name}" for node in lowered.find_all(exp.Column)
                           if node.name == name and node.table), name)
        elif unresolved:
            name, alias = unresolved.group(1), unresolved.group(2)
            column = f"{alias}.{name}" if alias else name
        else:
            return
        hint = unknown_column_hint(sql, column, columns, policy.dialect)
        _reject(hint or f"字段不存在：{column}。")
    except Exception:
        return


def _existing_limit(statement: exp.Expression) -> int | None:
    limit = statement.args.get("limit")
    if not isinstance(limit, exp.Limit):
        return None
    expression = limit.expression
    if isinstance(expression, exp.Literal) and expression.is_int:
        return int(expression.this)
    return None


def validate_and_limit(sql: str, max_rows: int, policy: SQLPolicy) -> SafeSQL:
    """Check that ``sql`` is one safe read of the allowed tables, and that every column
    exists when the policy knows the columns."""
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        _reject("SQL 为空")

    for pattern, reason in BLOCKED_PATTERNS:
        if pattern.search(cleaned):
            _reject(reason)

    try:
        statements = sqlglot.parse(cleaned, read=policy.dialect)
    except sqlglot.errors.ParseError as exc:
        raise SQLValidationError(f"SQL 无法解析：{exc}") from exc

    statements = [statement for statement in statements if statement is not None]
    if len(statements) != 1:
        _reject("只允许执行单条查询")

    statement = statements[0]
    _check_statement_type(statement)
    _check_tables(statement, policy)
    _check_functions(statement, cleaned)
    if policy.columns:
        _check_columns(statement, cleaned, policy, policy.columns)

    original_limit = _existing_limit(statement)
    limit_applied = False
    if original_limit is None or original_limit > max_rows:
        statement = statement.limit(max_rows)
        limit_applied = True

    return SafeSQL(sql=statement.sql(dialect=policy.dialect), limit_applied=limit_applied,
                   original_limit=original_limit)