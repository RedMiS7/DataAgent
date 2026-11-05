from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from agentscope.agent import Agent, ReActConfig
from agentscope.message import Msg, TextBlock

from ..agent.answer import verify_summary
from ..agent.pipeline import extract_text
from ..agent.scope_model import build_chat_model
from ..agent.tools import AgentRunContext, build_breakdown_toolkit
from ..config import Settings
from ..nl2sql.executor import execute_sql
from ..nl2sql.time_parser import TimeRange, parse_time_range
from ..safety.validator import validate_and_limit

logger = logging.getLogger(__name__)

ATTRIBUTION_SYSTEM_PROMPT = """你是电商经营分析助手，负责解释退货率变化的原因。

要求：
1. 必须调用 refund_breakdown 工具，分别查看 product、channel、carrier 三个维度。
2. 结论不超过 120 个字符，只使用工具返回的数字，不要编造。
3. 指出影响最大的 1～2 个因素和对应维度，不要输出 Markdown。
"""


@dataclass(slots=True)
class Factor:
    dimension: str
    name: str
    current_refund: float
    previous_refund: float
    delta: float
    share: float


@dataclass(slots=True)
class AttributionResult:
    metric: str
    current: dict[str, str]
    previous: dict[str, str]
    current_rate: float
    previous_rate: float
    delta_points: float
    total_current_refund: float
    total_previous_refund: float
    factors: list[Factor] = field(default_factory=list)
    summary: str = ""
    summary_source: str = "template"
    sql: list[str] = field(default_factory=list)


def previous_period(current: TimeRange) -> TimeRange:
    length = max(1, (current.end - current.start).days)
    end = current.start
    start = end - timedelta(days=length)
    return TimeRange(start, end, f"{start.isoformat()} 至 {end.isoformat()}")


def _totals_sql(settings: Settings, current: TimeRange, previous: TimeRange) -> str:
    return f"""
SELECT
  (SELECT COALESCE(SUM(refund_amount), 0) FROM safe.refunds
   WHERE status = 'completed' AND completed_at >= '{current.start.isoformat()}'
     AND completed_at < '{current.end.isoformat()}') AS cur_refund,
  (SELECT COALESCE(SUM(refund_amount), 0) FROM safe.refunds
   WHERE status = 'completed' AND completed_at >= '{previous.start.isoformat()}'
     AND completed_at < '{previous.end.isoformat()}') AS prev_refund,
  (SELECT COALESCE(SUM(oi.quantity * oi.unit_price - oi.discount_amount), 0)
   FROM safe.orders o JOIN safe.order_items oi ON oi.order_id = o.id
   WHERE o.status <> 'cancelled' AND o.paid_at >= '{current.start.isoformat()}'
     AND o.paid_at < '{current.end.isoformat()}') AS cur_gmv,
  (SELECT COALESCE(SUM(oi.quantity * oi.unit_price - oi.discount_amount), 0)
   FROM safe.orders o JOIN safe.order_items oi ON oi.order_id = o.id
   WHERE o.status <> 'cancelled' AND o.paid_at >= '{previous.start.isoformat()}'
     AND o.paid_at < '{previous.end.isoformat()}') AS prev_gmv
""".strip()


def _product_sql(current: TimeRange, previous: TimeRange) -> str:
    return f"""
WITH cur AS (
  SELECT product_id AS id, SUM(refund_amount) AS v FROM safe.refunds
  WHERE status = 'completed' AND completed_at >= '{current.start.isoformat()}'
    AND completed_at < '{current.end.isoformat()}'
  GROUP BY product_id
), prev AS (
  SELECT product_id AS id, SUM(refund_amount) AS v FROM safe.refunds
  WHERE status = 'completed' AND completed_at >= '{previous.start.isoformat()}'
    AND completed_at < '{previous.end.isoformat()}'
  GROUP BY product_id
)
SELECT d.name AS name, COALESCE(cur.v, 0) AS cur_refund, COALESCE(prev.v, 0) AS prev_refund
FROM safe.products d
LEFT JOIN cur ON cur.id = d.id
LEFT JOIN prev ON prev.id = d.id
WHERE COALESCE(cur.v, 0) <> 0 OR COALESCE(prev.v, 0) <> 0
ORDER BY ABS(COALESCE(cur.v, 0) - COALESCE(prev.v, 0)) DESC
LIMIT 10
""".strip()


def _channel_sql(current: TimeRange, previous: TimeRange) -> str:
    return f"""
WITH cur AS (
  SELECT o.channel_id AS id, SUM(r.refund_amount) AS v
  FROM safe.refunds r JOIN safe.orders o ON o.id = r.order_id
  WHERE r.status = 'completed' AND r.completed_at >= '{current.start.isoformat()}'
    AND r.completed_at < '{current.end.isoformat()}'
  GROUP BY o.channel_id
), prev AS (
  SELECT o.channel_id AS id, SUM(r.refund_amount) AS v
  FROM safe.refunds r JOIN safe.orders o ON o.id = r.order_id
  WHERE r.status = 'completed' AND r.completed_at >= '{previous.start.isoformat()}'
    AND r.completed_at < '{previous.end.isoformat()}'
  GROUP BY o.channel_id
)
SELECT d.name AS name, COALESCE(cur.v, 0) AS cur_refund, COALESCE(prev.v, 0) AS prev_refund
FROM safe.channels d
LEFT JOIN cur ON cur.id = d.id
LEFT JOIN prev ON prev.id = d.id
WHERE COALESCE(cur.v, 0) <> 0 OR COALESCE(prev.v, 0) <> 0
ORDER BY ABS(COALESCE(cur.v, 0) - COALESCE(prev.v, 0)) DESC
LIMIT 10
""".strip()


def _carrier_sql(current: TimeRange, previous: TimeRange) -> str:
    return f"""
WITH cur AS (
  SELECT s.carrier AS name, SUM(r.refund_amount) AS v
  FROM safe.refunds r
  JOIN (SELECT order_id, MAX(carrier) AS carrier FROM safe.shipments GROUP BY order_id) s
    ON s.order_id = r.order_id
  WHERE r.status = 'completed' AND r.completed_at >= '{current.start.isoformat()}'
    AND r.completed_at < '{current.end.isoformat()}'
  GROUP BY s.carrier
), prev AS (
  SELECT s.carrier AS name, SUM(r.refund_amount) AS v
  FROM safe.refunds r
  JOIN (SELECT order_id, MAX(carrier) AS carrier FROM safe.shipments GROUP BY order_id) s
    ON s.order_id = r.order_id
  WHERE r.status = 'completed' AND r.completed_at >= '{previous.start.isoformat()}'
    AND r.completed_at < '{previous.end.isoformat()}'
  GROUP BY s.carrier
)
SELECT d.carrier AS name, COALESCE(cur.v, 0) AS cur_refund, COALESCE(prev.v, 0) AS prev_refund
FROM (SELECT DISTINCT carrier FROM safe.shipments) d
LEFT JOIN cur ON cur.name = d.carrier
LEFT JOIN prev ON prev.name = d.carrier
WHERE COALESCE(cur.v, 0) <> 0 OR COALESCE(prev.v, 0) <> 0
ORDER BY ABS(COALESCE(cur.v, 0) - COALESCE(prev.v, 0)) DESC
LIMIT 10
""".strip()


def _run(settings: Settings, sql: str) -> list[list]:
    safe = validate_and_limit(sql, settings.sql_max_rows)
    return execute_sql(settings, safe).rows


def _build_factors(settings: Settings, sql: str, dimension: str,
                   total_delta: float) -> list[Factor]:
    factors: list[Factor] = []
    for row in _run(settings, sql):
        name, current, previous = str(row[0]), float(row[1]), float(row[2])
        delta = current - previous
        share = delta / total_delta * 100 if total_delta else 0.0
        factors.append(Factor(dimension=dimension, name=name, current_refund=round(current, 2),
                              previous_refund=round(previous, 2), delta=round(delta, 2),
                              share=round(share, 1)))
    factors.sort(key=lambda item: item.delta, reverse=True)
    return factors


def _rate(refund: float, gmv: float) -> float:
    return round(refund / gmv * 100, 2) if gmv else 0.0


def _agent_summary(settings: Settings, question: str, result: AttributionResult,
                   factors: list[Factor]) -> str:
    if settings.llm_provider.lower() == "fake":
        return ""
    model = build_chat_model(settings)
    if model is None:
        return ""

    def breakdown(dimension: str) -> dict[str, Any]:
        rows = [factor for factor in factors if factor.dimension == dimension][:8]
        return {
            "dimension": dimension,
            "rows": [
                {"name": factor.name, "current_refund": factor.current_refund,
                 "previous_refund": factor.previous_refund, "delta": factor.delta,
                 "share": factor.share}
                for factor in rows
            ],
        }

    context = AgentRunContext(settings=settings, retriever=None)  # type: ignore[arg-type]
    agent = Agent(
        name="attribution",
        system_prompt=ATTRIBUTION_SYSTEM_PROMPT,
        model=model,
        toolkit=build_breakdown_toolkit(context, breakdown),
        react_config=ReActConfig(max_iters=6),
    )
    prompt = (
        f"{question}\n\n当前期退货率 {result.current_rate}%，"
        f"对比期 {result.previous_rate}%，变化 {result.delta_points} 个百分点。"
        "请调用 refund_breakdown 查看三个维度后给出结论。"
    )
    reply = asyncio.run(agent.reply(
        Msg(name="user", content=[TextBlock(text=prompt)], role="user")))
    text = extract_text(reply)
    if not text:
        return ""

    check_rows: list[list[Any]] = [[factor.name, factor.delta, factor.share]
                                   for factor in factors]
    check_rows += [[result.current_rate], [result.previous_rate], [result.delta_points]]
    return text if verify_summary(text, check_rows) else ""


def attribute_return_rate(settings: Settings, question: str) -> AttributionResult:
    current = parse_time_range(question, settings.as_of_date)
    previous = previous_period(current)

    totals = _run(settings, _totals_sql(settings, current, previous))[0]
    cur_refund, prev_refund, cur_gmv, prev_gmv = (float(value) for value in totals)
    total_delta = cur_refund - prev_refund

    factors: list[Factor] = []
    factors.extend(_build_factors(settings, _product_sql(current, previous), "product", total_delta))
    factors.extend(_build_factors(settings, _channel_sql(current, previous), "channel", total_delta))
    factors.extend(_build_factors(settings, _carrier_sql(current, previous), "carrier", total_delta))

    current_rate = _rate(cur_refund, cur_gmv)
    previous_rate = _rate(prev_refund, prev_gmv)
    delta_points = round(current_rate - previous_rate, 2)

    result = AttributionResult(
        metric="退货率",
        current=current.as_dict(),
        previous={"start": previous.start.isoformat(), "end_exclusive": previous.end.isoformat(),
                  "label": previous.label},
        current_rate=current_rate,
        previous_rate=previous_rate,
        delta_points=delta_points,
        total_current_refund=round(cur_refund, 2),
        total_previous_refund=round(prev_refund, 2),
        factors=factors,
    )

    top_products = [f for f in factors if f.dimension == "product"][:3]
    top_channels = [f for f in factors if f.dimension == "channel"][:2]
    if delta_points >= 0:
        trend = f"退货率从 {previous_rate}% 上升到 {current_rate}%，上升 {abs(delta_points)} 个百分点"
    else:
        trend = f"退货率从 {previous_rate}% 下降到 {current_rate}%，下降 {abs(delta_points)} 个百分点"
    details = []
    for factor in top_products:
        details.append(
            f"商品「{factor.name}」退款金额变化 {factor.delta:+.2f} 元，"
            f"占整体变化 {factor.share:+.1f}%")
    for factor in top_channels:
        details.append(
            f"渠道「{factor.name}」退款金额变化 {factor.delta:+.2f} 元，"
            f"占整体变化 {factor.share:+.1f}%")
    result.summary = trend + "。" + ("；".join(details) if details else "各维度变化不明显。")
    try:
        agent_summary = _agent_summary(settings, question, result, factors)
    except Exception:
        logger.exception("attribution agent failed, falling back to template summary")
        agent_summary = ""
    if agent_summary:
        result.summary = agent_summary
        result.summary_source = "agent"
    result.sql = [
        _totals_sql(settings, current, previous),
        _product_sql(current, previous),
        _channel_sql(current, previous),
        _carrier_sql(current, previous),
    ]
    return result