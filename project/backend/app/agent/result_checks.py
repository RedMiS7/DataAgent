"""Check query values without changing the result or judging the model's conclusion."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from ..nl2sql.executor import ExecutionResult

SHARE_LABEL = re.compile(
    r"占比|份额|通过率|转化率|转换率|发生率|欺诈率|复购率"
    r"|(?:^|_)(?:share|proportion|approval_rate|conversion_rate|fraud_rate)(?:$|_)",
    re.IGNORECASE,
)
MEASURE_LABEL = re.compile(
    r"件数|数量|销量|金额|总额|单价|销售额|成交额|退款额|成本|花费|费用"
    r"|(?:^|_)(?:quantity|qty|amount|price|cost|spend)(?:$|_)",
    re.IGNORECASE,
)
SIGNED_LABEL = re.compile(
    r"增长|变化|变动|增量|差额|差值|净|利润|毛利|周转|回报|产出比|同比|环比|调整|冲销"
    r"|(?:^|_)(?:roi|net|profit|margin|change|delta|growth|adjustment|balance)(?:$|_)",
    re.IGNORECASE,
)
EMPTY_RESULT = "查询没有返回数据，请核对时间范围和筛选条件。"


@dataclass(frozen=True, slots=True)
class GroupTotal:
    sql: str
    # The primary result's column indexes, and whether each is an exact count.
    measures: tuple[tuple[int, bool], ...]


def _values(result: ExecutionResult, index: int) -> list[Decimal] | None:
    values = []
    for row in result.rows:
        if index >= len(row):
            return None
        value = row[index]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            return None
        number = Decimal(str(value))
        if not number.is_finite():
            return None
        values.append(number)
    return values


def _bare(node: exp.Expression) -> exp.Expression:
    while isinstance(node, (exp.Paren, exp.Alias)):
        node = node.this
    return node


def _is_literal(node: exp.Expression | None, *values: int) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string and Decimal(node.this) in values


def _metric_expression(node: exp.Expression) -> exp.Expression:
    node = _bare(node)
    while True:
        if isinstance(node, (exp.Cast, exp.Round)):
            node = _bare(node.this)
        elif isinstance(node, exp.Coalesce) and all(_is_literal(item, 0)
                                                    for item in node.expressions):
            node = _bare(node.this)
        else:
            return node


def _scaled(node: exp.Expression) -> tuple[exp.Expression, int]:
    node = _metric_expression(node)
    if isinstance(node, exp.Mul):
        for number, value in ((node.this, node.expression), (node.expression, node.this)):
            if _is_literal(_bare(number), 100, 1):
                return _metric_expression(value), int(Decimal(_bare(number).this))
    return node, 1


def _is_indicator(node: exp.Expression) -> bool:
    node = _metric_expression(node)
    if isinstance(node, (exp.Predicate, exp.And, exp.Or, exp.Not, exp.Boolean)):
        return True
    if isinstance(node, exp.Case):
        branches = [part.args.get("true") for part in node.args["ifs"]]
        branches.append(node.args.get("default"))
    elif isinstance(node, exp.If):
        branches = [node.args.get("true"), node.args.get("false")]
    else:
        return False
    return all(item is None or isinstance(item, (exp.Null, exp.Boolean))
               or _is_literal(item, 0, 1) for item in branches)


def _share_max(node: exp.Expression, name: str) -> int | None:
    """Recognize a subset share by its label or the conditional-count expression."""
    ratio, scale = _scaled(node)
    if isinstance(ratio, exp.Avg) and _is_indicator(ratio.this):
        return scale
    if not isinstance(ratio, exp.Div):
        return None
    numerator, numerator_scale = _scaled(ratio.this)
    denominator = _metric_expression(ratio.expression)
    if isinstance(denominator, exp.Nullif) and _is_literal(denominator.expression, 0):
        denominator = _metric_expression(denominator.this)
    subset = (isinstance(denominator, exp.Count) and isinstance(denominator.this, exp.Star)
              and (isinstance(numerator, exp.Count)
                   or (isinstance(numerator, exp.Sum) and _is_indicator(numerator.this))))
    if subset or SHARE_LABEL.search(name):
        return scale * numerator_scale
    return None


def _is_measure_column(node: exp.Expression, name: str = "") -> bool:
    value = _bare(node)
    return isinstance(value, exp.Column) and not SIGNED_LABEL.search(value.name) and bool(
        MEASURE_LABEL.search(value.name) or MEASURE_LABEL.search(name))


def _is_simple_measure(node: exp.Expression, name: str) -> bool:
    value = _metric_expression(node)
    if isinstance(value, exp.Count):
        return True
    if isinstance(value, (exp.Sum, exp.Avg, exp.Min, exp.Max)):
        value = _bare(value.this)
    if isinstance(value, exp.Mul):
        return _is_measure_column(value.this) and _is_measure_column(value.expression)
    return _is_measure_column(value, name)


def _group_total(statement: exp.Select, dialect: str) -> GroupTotal | None:
    group = statement.args.get("group")
    if not isinstance(group, exp.Group) or not group.expressions:
        return None
    if any(value for key, value in group.args.items() if key != "expressions"):
        return None
    if any(statement.args.get(key) is not None
           for key in ("limit", "offset", "having", "qualify", "distinct")):
        return None
    if statement.find(exp.Rand):
        return None

    projections = []
    measures = []
    for index, projection in enumerate(statement.expressions):
        value = _bare(projection)
        aggregate = value
        if isinstance(value, exp.Coalesce) and all(_is_literal(item, 0)
                                                   for item in value.expressions):
            aggregate = _bare(value.this)
        if isinstance(aggregate, (exp.Count, exp.Sum)) and not aggregate.find(
                exp.Distinct, exp.Subquery, exp.Window):
            projections.append(exp.alias_(value, f"check_total_{index}"))
            measures.append((index, isinstance(aggregate, exp.Count)))
    if not projections:
        return None

    total = statement.select(*projections, append=False)
    total.set("group", None)
    total.set("order", None)
    return GroupTotal(total.sql(dialect=dialect), tuple(measures))


def inspect_result(sql: str, result: ExecutionResult, dialect: str, *,
                   expects_data: bool, can_check_total: bool) -> tuple[list[str], GroupTotal | None]:
    if not result.rows:
        return ([EMPTY_RESULT] if expects_data else []), None

    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except SqlglotError:
        return [], None
    if not isinstance(statement, exp.Select) or len(statement.expressions) != len(result.columns):
        return [], None

    warnings = []
    for index, (projection, name) in enumerate(zip(statement.expressions, result.columns,
                                                   strict=True)):
        if SIGNED_LABEL.search(name):
            continue
        values = _values(result, index)
        if not values:
            continue
        upper = _share_max(projection, name)
        if upper is not None:
            if any(value < 0 or value > upper for value in values):
                bounds = "0～100%" if upper == 100 else f"0～{upper}（小数）"
                warnings.append(f"「{name}」超出整体份额的范围 {bounds}，请核对分子、分母和单位。")
        elif _is_simple_measure(projection, name) and min(values) < 0:
            warnings.append(f"「{name}」出现负值，请核对数量或金额的口径。")

    total = _group_total(statement, dialect) if can_check_total and not result.truncated else None
    return warnings, total


def compare_group_total(group: ExecutionResult, total: ExecutionResult,
                        check: GroupTotal) -> list[str]:
    if len(total.rows) != 1 or len(total.rows[0]) != len(check.measures):
        return []
    warnings = []
    for position, (index, exact_count) in enumerate(check.measures):
        values = _values(group, index)
        overall = _values(total, position)
        if not values or not overall:
            continue
        subtotal, expected = sum(values, Decimal(0)), overall[0]
        tolerance = Decimal(0) if exact_count else max(
            Decimal("0.000001"), abs(expected) * Decimal("0.000000000001"))
        if abs(subtotal - expected) > tolerance:
            warnings.append(f"「{group.columns[index]}」的分组合计为 {subtotal}，"
                            f"同口径总数为 {expected}，请核对分组、筛选或两次查询之间的数据变化。")
    return warnings
