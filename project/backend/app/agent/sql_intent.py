"""Check a SQL against how its question was understood, before it runs (plan 2.3).

Each check names one way a SQL can answer a different question than the one asked: the
wrong period, a metric without the tables its definition needs, a missing group or
filter, a result of the wrong shape, or rows counted after a join multiplied them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

import sqlglot
from sqlglot import exp

from ..nl2sql.time_parser import TimeRange
from ..semantic.models import Metric, SchemaItem
from ..semantic.schema_context import bare_name, hint_keys, table_names
from .intent import Intent

CheckKind = Literal["time", "metric_tables", "ratio", "group", "filter", "shape", "fan_out"]
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# A formula with one of these computes a ratio, and its SQL must divide.
DIVISION = ("/", "除以", "÷")
# Prefix of the message the model gets; the evaluation counts blocked SQL by it.
BLOCK_PREFIX = "SQL 和问题的理解不一致"
WARNING_PREFIX = "SQL 可能和问题不一致"


@dataclass(frozen=True, slots=True)
class OneToMany:
    """One row of ``one`` matches many rows of ``many``: joining them repeats ``one``."""

    one: str
    one_column: str
    many: str
    many_column: str


@dataclass(slots=True)
class SQLChecks:
    """What the SQL of one question is checked against."""

    # The period and baseline the question names; None when it needs no period.
    time_range: TimeRange | None = None
    # True when the user named the period, so a SQL without dates is wrong too.
    time_named: bool = False
    # Each metric of the semantic layer the question asks for, with its tables.
    metric_tables: dict[str, list[str]] = field(default_factory=dict)
    ratio_metrics: list[str] = field(default_factory=list)
    shape: str = ""
    top_n: int | None = None
    # (table, column) of each dimension; table is "" when only the wording is known.
    dimensions: list[tuple[str, str, str]] = field(default_factory=list)
    filter_values: list[str] = field(default_factory=list)
    relations: list[OneToMany] = field(default_factory=list)
    # In analysis mode each query looks at one part, so only the join check applies.
    only_fan_out: bool = False


@dataclass(frozen=True, slots=True)
class Mismatch:
    kind: CheckKind
    message: str


def one_to_many(items: Iterable[SchemaItem], schema: str) -> list[OneToMany]:
    """Joins that repeat rows, from the join hints of the semantic layer.

    A hint links two columns in either direction; the side whose column is ``id`` is the
    one side. Links between two non-id columns say nothing about which side repeats.
    """
    relations: set[OneToMany] = set()
    for key in hint_keys(items, f"{schema}."):
        if key.column == "id" and key.ref_column != "id":
            relations.add(OneToMany(key.table, key.column, key.ref_table, key.ref_column))
        elif key.ref_column == "id" and key.column != "id":
            relations.add(OneToMany(key.ref_table, key.ref_column, key.table, key.column))
    return sorted(relations, key=lambda item: (item.one, item.many))


def _is_ratio(metric: Metric) -> bool:
    return any(mark in metric.formula for mark in DIVISION)


def build_checks(intent: Intent | None, time_range: TimeRange, metrics: Sequence[Metric],
                 items: Sequence[SchemaItem], schema: str, mode: str) -> SQLChecks:
    """The checks for one question; without an intent, only the join check."""
    checks = SQLChecks(relations=one_to_many(items, schema), only_fan_out=mode != "query")
    if intent is None or checks.only_fan_out:
        return checks
    if intent.time_range.needed:
        checks.time_range = time_range
        checks.time_named = bool(intent.time_range.text)
    by_name = {metric.name: metric for metric in metrics}
    for wanted in intent.metrics:
        metric = by_name.get(wanted.name)
        if metric is None:
            continue
        tables = [bare_name(name, f"{schema}.") for name in metric.related_tables.split(",")
                  if name.strip()]
        if tables:
            checks.metric_tables[metric.name] = tables
        if _is_ratio(metric):
            checks.ratio_metrics.append(metric.name)
    checks.shape = intent.result_shape.type
    checks.top_n = intent.result_shape.n
    for dimension in intent.dimensions:
        parts = dimension.field.lower().split(".")
        if len(parts) >= 2:
            checks.dimensions.append((parts[-2], parts[-1], dimension.term or dimension.field))
    checks.filter_values = [item.value for item in intent.filters if item.field and item.value]
    return checks


def _outer_select(statement: exp.Expression) -> exp.Select | None:
    return statement if isinstance(statement, exp.Select) else None


def _own_tables(select: exp.Select) -> dict[str, str]:
    """The tables read by this SELECT itself, not its subqueries: {alias: table}."""
    tables: dict[str, str] = {}
    for table in select.find_all(exp.Table):
        if table.find_ancestor(exp.Select) is select and table.name:
            tables[table.alias_or_name.lower()] = table.name.lower()
    return tables


def _own(select: exp.Select, node: exp.Expression) -> bool:
    return node.find_ancestor(exp.Select) is select


def _group_columns(select: exp.Select) -> list[tuple[str | None, str]]:
    """(table, column) of each column the SELECT groups by. GROUP BY 1 and GROUP BY an
    alias point at the select list; a table is None when it cannot be told."""
    group = select.args.get("group")
    if group is None:
        return []
    aliases = {projection.alias_or_name.lower(): projection for projection in select.expressions}
    tables = _own_tables(select)
    columns: list[tuple[str | None, str]] = []
    for item in group.expressions:
        target: exp.Expression = item
        if isinstance(item, exp.Literal) and item.is_int:
            position = int(item.this) - 1
            if 0 <= position < len(select.expressions):
                target = select.expressions[position]
        elif isinstance(item, exp.Column) and not item.table and item.name.lower() in aliases:
            target = aliases[item.name.lower()]
        for column in target.find_all(exp.Column):
            table = tables.get(column.table.lower()) if column.table else None
            columns.append((table, column.name.lower()))
    return columns


def _limit(select: exp.Select) -> int | None:
    limit = select.args.get("limit")
    value = limit.expression if limit is not None else None
    if isinstance(value, exp.Literal) and value.is_int:
        return int(value.this)
    return None


def _check_time(sql: str, checks: SQLChecks) -> list[Mismatch]:
    time_range = checks.time_range
    if time_range is None:
        return []
    dates = set(DATE.findall(sql))
    if not dates:
        if checks.time_named:
            return [Mismatch("time", f"问题限定了时间（{time_range.label}），SQL 里没有时间条件。")]
        return []
    wanted = [time_range, *([time_range.baseline] if time_range.baseline else [])]
    for period in wanted:
        last_day = period.end - timedelta(days=1)
        has_start = period.start.isoformat() in dates
        has_end = bool(dates & {period.end.isoformat(), last_day.isoformat()})
        if not (has_start and has_end):
            return [Mismatch("time", (
                f"时间条件和问题的时间不一致：{period.label} 是 >= '{period.start.isoformat()}' "
                f"且 < '{period.end.isoformat()}'。"))]
    return []


def _check_metric_tables(sql: str, schema: str, checks: SQLChecks) -> list[Mismatch]:
    used = set(table_names(sql, schema))
    found: list[Mismatch] = []
    for metric, tables in checks.metric_tables.items():
        missing = [table for table in tables if table not in used]
        if missing:
            found.append(Mismatch("metric_tables", (
                f"指标“{metric}”的口径要用到 {'、'.join(tables)}，SQL 没有用到 "
                f"{'、'.join(missing)}。按【指标口径】的公式写。")))
    return found


def _check_ratio(statement: exp.Expression, checks: SQLChecks) -> list[Mismatch]:
    wanted = checks.ratio_metrics or (["比率"] if checks.shape == "ratio" else [])
    if not wanted or any(True for _ in statement.find_all(exp.Div)):
        return []
    return [Mismatch("ratio", (
        f"问的是{'、'.join(wanted)}，是一个比值，SQL 没有做除法。把比值写进 SQL 算出来，"
        "不要只查分子和分母。"))]


def _check_group(select: exp.Select | None, checks: SQLChecks) -> list[Mismatch]:
    if not checks.dimensions:
        return []
    columns = _group_columns(select) if select is not None else []
    missing: list[str] = []
    for table, column, label in checks.dimensions:
        covered = any(found_table == table or (found_table is None and found_column == column)
                      for found_table, found_column in columns)
        if not covered:
            missing.append(f"{label}（{table}.{column}）")
    if not missing:
        return []
    return [Mismatch("group", f"问题要按 {'、'.join(missing)} 分组，SQL 没有按它 GROUP BY。")]


def _check_filters(statement: exp.Expression, checks: SQLChecks) -> list[Mismatch]:
    literals = [literal.this for literal in statement.find_all(exp.Literal) if literal.is_string]
    missing = [value for value in checks.filter_values
               if not any(value in literal for literal in literals)]
    if not missing:
        return []
    return [Mismatch("filter", f"问题限定了 {'、'.join(missing)}，SQL 的条件里没有这个值。")]


def _check_shape(select: exp.Select | None, checks: SQLChecks) -> list[Mismatch]:
    if select is None:
        return []
    grouped = select.args.get("group") is not None
    if checks.shape == "top_n":
        limit = _limit(select)
        wanted = checks.top_n
        if select.args.get("order") is None or limit is None:
            return [Mismatch("shape", (
                f"问的是排名前 {wanted or 'N'} 的结果，SQL 要有 ORDER BY 和 LIMIT。"))]
        if wanted is not None and limit > wanted:
            return [Mismatch("shape", f"问的是前 {wanted} 个，SQL 的 LIMIT 是 {limit}。")]
    if checks.shape == "single" and grouped and not checks.dimensions:
        return [Mismatch("shape", "问的是一个数值，SQL 却按字段分组，会返回多行。")]
    if checks.shape == "trend" and not grouped:
        return [Mismatch("shape", "问的是趋势，SQL 要按时间分组。")]
    return []


def _check_fan_out(statement: exp.Expression, relations: Sequence[OneToMany]) -> list[Mismatch]:
    """A count of the one side after joining its many side counts it more than once."""
    found: list[Mismatch] = []
    for select in statement.find_all(exp.Select):
        tables = _own_tables(select)
        present = set(tables.values())
        source = select.args.get("from_")
        driving = (source.this.name.lower()
                   if source is not None and isinstance(source.this, exp.Table) else "")
        for relation in relations:
            if relation.one not in present or relation.many not in present:
                continue
            for count in select.find_all(exp.Count):
                if not _own(select, count) or isinstance(count.this, exp.Distinct):
                    continue
                counted = count.this
                if isinstance(counted, exp.Star):
                    wrong = driving == relation.one
                else:
                    wrong = (isinstance(counted, exp.Column)
                             and tables.get(counted.table.lower()) == relation.one)
                if wrong:
                    found.append(Mismatch("fan_out", (
                        f"{relation.one} 关联 {relation.many} 后，{relation.one} 的一行会变成多行，"
                        f"{count.sql()} 会多数。数 {relation.one} 要用 "
                        f"COUNT(DISTINCT {relation.one}.{relation.one_column})。")))
                    return found
    return found


def check_sql(sql: str, checks: SQLChecks, schema: str, dialect: str) -> list[Mismatch]:
    """Every way the SQL departs from the question; empty when it does not, or when the
    SQL cannot be parsed (the safety check has passed it, so it runs as it is)."""
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return []
    found = _check_fan_out(statement, checks.relations)
    if checks.only_fan_out:
        return found
    select = _outer_select(statement)
    return [*_check_time(sql, checks), *_check_metric_tables(sql, schema, checks),
            *_check_ratio(statement, checks), *_check_group(select, checks),
            *_check_filters(statement, checks), *_check_shape(select, checks), *found]


def block_message(mismatches: Sequence[Mismatch]) -> str:
    """What the model is told, like a SQL the safety check turned back."""
    lines = "\n".join(f"- {item.message}" for item in mismatches)
    return f"{BLOCK_PREFIX}，没有执行：\n{lines}\n按问题的理解修改 SQL 后重新调用 run_sql。"


def warning_text(mismatch: Mismatch) -> str:
    return f"{WARNING_PREFIX}：{mismatch.message}"


__all__ = [
    "BLOCK_PREFIX", "WARNING_PREFIX", "Mismatch", "OneToMany", "SQLChecks", "block_message",
    "build_checks", "check_sql", "one_to_many", "warning_text",
]
