"""Build the read-only query for a two-period metric breakdown."""

from __future__ import annotations

from datetime import date

import sqlglot
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp
from sqlglot.errors import SqlglotError

from ..nl2sql.time_parser import TimeRange, previous_period


class MetricPart(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    from_sql: str = Field(min_length=1, description="FROM 后的表和 JOIN，不含 FROM 关键字")
    expression: str = Field(min_length=1, description="聚合表达式，例如 SUM(t.amount)")
    time_column: str = Field(min_length=1, description="用于归属日期的字段或表达式")
    dimension: str = Field(min_length=1, description="分组表达式，分子和分母用含义相同的维度值")
    where: str = Field(default="", description="可选筛选条件，不含 WHERE 关键字和两期日期")


class BreakdownPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date = Field(description="开始日期（含），格式 YYYY-MM-DD")
    end_exclusive: date = Field(description="结束日期（不含），格式 YYYY-MM-DD")


class BreakdownRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    numerator: MetricPart
    denominator: MetricPart | None = None
    current: BreakdownPeriod | None = None
    previous: BreakdownPeriod | None = None
    purpose: str = ""


class BreakdownError(ValueError):
    pass


DIMENSION_COUNT = "维度总数"
CTE_NAMES = {"mb_values", "mb_periods", "mb_changes", "mb_ranked"}


def _period(value: BreakdownPeriod | None, default: TimeRange | None) -> BreakdownPeriod:
    if value is None:
        if default is None:
            raise BreakdownError("缺少时间范围，请填写 start 和 end_exclusive")
        value = BreakdownPeriod(start=default.start, end_exclusive=default.end)
    if value.start >= value.end_exclusive:
        raise BreakdownError("时间范围的 start 必须早于 end_exclusive")
    return value


def _select(sql: str, dialect: str, label: str, allowed: set[str]) -> exp.Select:
    """Read one fragment inside a SELECT and prevent it from adding top-level clauses."""
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except SqlglotError as exc:
        raise BreakdownError(f"{label} 无法解析：{exc}") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise BreakdownError(f"{label} 只能填写单个 SQL 片段")
    select = statements[0]
    if any(value for key, value in select.args.items() if key not in allowed):
        raise BreakdownError(f"{label} 含有不支持的 SQL 子句")
    # Generated CTE names must never hide a physical table from the SQL policy.
    if select.find(exp.CTE) or any(t.name.lower() in CTE_NAMES for t in select.find_all(exp.Table)):
        raise BreakdownError(f"{label} 请使用表或派生子查询，不使用 WITH 或工具保留的表名")
    return select


def _expression(text: str, dialect: str, label: str) -> exp.Expression:
    select = _select(f"SELECT {text}", dialect, label, {"expressions"})
    values = select.expressions
    if len(values) != 1 or isinstance(values[0], (exp.Alias, exp.Star)):
        raise BreakdownError(f"{label} 必须是一个不带别名的表达式")
    return values[0].copy()


def _queries(part: MetricPart, side: str, periods: tuple[BreakdownPeriod, BreakdownPeriod],
             dialect: str, expression: exp.Expression) -> list[str]:
    source = _select(f"SELECT 1 FROM {part.from_sql}", dialect, "from_sql",
                     {"expressions", "from_", "joins"})
    # A scalar subquery or a window function alone does not aggregate the source rows.
    if not any(agg.find_ancestor(exp.Select, exp.Window) is None
               for agg in expression.find_all(exp.AggFunc)):
        raise BreakdownError("expression 必须包含聚合函数，例如 SUM 或 COUNT")
    dimension = _expression(part.dimension, dialect, "dimension")
    time = _expression(part.time_column, dialect, "time_column")
    where = (_select(f"SELECT 1 WHERE {part.where}", dialect, "where",
                     {"expressions", "where"}).args["where"].this if part.where else None)
    sqls: list[str] = []
    for name, period in zip(("current", "previous"), periods, strict=True):
        filters = exp.and_(exp.GTE(this=time.copy(), expression=exp.Literal.string(
            period.start.isoformat())), exp.LT(this=time.copy(), expression=exp.Literal.string(
                period.end_exclusive.isoformat())))
        if where is not None:
            filters = exp.and_(where, filters)
        base = source.select(
            exp.alias_(exp.Literal.string(f"{side}_{name}"), "bucket"),
            exp.alias_(expression, "amount"), append=False,
        ).where(filters)
        sqls.extend([
            base.select("1 AS is_total", "NULL AS dimension_value").sql(dialect=dialect),
            base.select("0 AS is_total", exp.alias_(dimension, "dimension_value"))
            .group_by(dimension).sql(dialect=dialect),
        ])
    return sqls


def _period_value(side: str, period: str, zero_when_missing: bool) -> str:
    amount = f"MAX(CASE WHEN bucket = '{side}_{period}' THEN amount END)"
    if zero_when_missing:
        amount = f"COALESCE({amount}, 0)"
    return f"{amount} AS {side}_{period}"


def _share(side: str, additive: bool) -> str:
    if not additive:
        return "NULL"
    # Check reconciliation independently in both periods before allocating shares.
    return f"""CASE WHEN x.is_total = 0
        AND ABS(x.{side}_current_grouped - t.{side}_current) <= ABS(t.{side}_current) * 0.00000001
        AND ABS(x.{side}_previous_grouped - t.{side}_previous) <= ABS(t.{side}_previous) * 0.00000001
        THEN 100.0 * x.{side}_delta / NULLIF(t.{side}_delta, 0) END"""


def build_breakdown_sql(numerator: MetricPart, denominator: MetricPart | None,
                        current: BreakdownPeriod | None, previous: BreakdownPeriod | None,
                        default: TimeRange | None, dialect: str) -> str:
    periods = (_period(current, default), _period(
        previous, (default.baseline or previous_period(default)) if default else None))
    parts = {"n": numerator}
    if denominator is not None:
        parts["d"] = denominator
    queries = []
    zero_when_missing = {}
    additive = {}
    for side, part in parts.items():
        expression = _expression(part.expression, dialect, "expression")
        queries.extend(_queries(part, side, periods, dialect, expression))
        aggregate = expression.unnest()
        if isinstance(aggregate, exp.Coalesce) and aggregate.expressions == [exp.Literal.number(0)]:
            aggregate = aggregate.this.unnest()
        additive[side] = isinstance(aggregate, (exp.Sum, exp.Count)) and not aggregate.find(exp.Distinct)
        zero_when_missing[side] = additive[side] or isinstance(aggregate, exp.Count)

    pivot = ",\n".join(
        _period_value(side, period, zero_when_missing[side])
        for side in parts for period in ("current", "previous")
    )
    deltas = [f"{side}_current - {side}_previous AS {side}_delta" for side in parts]
    grouped = [
        f"SUM(CASE WHEN is_total = 0 THEN {side}_{period} ELSE 0 END) "
        f"OVER () AS {side}_{period}_grouped"
        for side in parts for period in ("current", "previous")
        if additive[side]
    ]
    changes = ",\n".join(deltas + grouped)
    shares = ",\n".join(f"{_share(side, additive[side])} AS {side}_share" for side in parts)
    values = "\nUNION ALL\n".join(queries)
    quote = "`" if dialect == "mysql" else '"'
    columns = {
        "CASE WHEN is_total = 1 THEN '总体' ELSE '分组' END": "层级",
        "dimension_value": "维度值",
        "n_current": "本期分子" if denominator else "本期值",
        "n_previous": "对比期分子" if denominator else "对比期值",
        "n_delta": "分子增量" if denominator else "增量",
        "ROUND(n_share, 2)": "分子增量贡献占比(%)" if denominator else "增量贡献占比(%)",
    }
    rank = "COALESCE(n_share, 0)"
    if denominator is not None:
        current_rate = "100.0 * n_current / NULLIF(d_current, 0)"
        previous_rate = "100.0 * n_previous / NULLIF(d_previous, 0)"
        columns.update({
            "d_current": "本期分母",
            "d_previous": "对比期分母",
            "d_delta": "分母增量",
            "ROUND(d_share, 2)": "分母增量贡献占比(%)",
            f"ROUND({current_rate}, 4)": "本期比率(%)",
            f"ROUND({previous_rate}, 4)": "对比期比率(%)",
            f"ROUND(({current_rate}) - ({previous_rate}), 4)": "比率变化(百分点)",
        })
        rank = f"""CASE WHEN COALESCE(d_share, 0) > {rank}
                    THEN d_share ELSE {rank} END"""
    columns["COUNT(*) OVER () - 1"] = DIMENSION_COUNT
    projection = ",\n".join(f"{expr} AS {quote}{alias}{quote}" for expr, alias in columns.items())
    return f"""WITH mb_values AS (
{values}
), mb_periods AS (
SELECT is_total, dimension_value, {pivot}
FROM mb_values GROUP BY is_total, dimension_value
), mb_changes AS (
SELECT mb_periods.*, {changes} FROM mb_periods
), mb_ranked AS (
SELECT x.*, {shares} FROM mb_changes x
CROSS JOIN mb_changes t WHERE t.is_total = 1
)
SELECT {projection}
FROM mb_ranked
ORDER BY is_total DESC, {rank} DESC, ABS(n_delta) DESC, dimension_value"""
