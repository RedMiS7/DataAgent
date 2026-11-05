from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..api.schemas import ChartSpec, MetricNote
from ..semantic.models import RetrievalBundle

TIME_MARKERS = ("date", "time", "day", "month", "year", "日期", "时间", "月份", "年")
CATEGORY_MARKERS = ("category", "channel", "province", "city", "类目", "渠道", "省份", "城市",
                    "商品", "product", "name")


def extract_sql(text: str) -> str:
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    candidate = candidate.strip()
    starts = [index for index in (candidate.upper().find("SELECT"), candidate.upper().find("WITH"))
              if index >= 0]
    if starts:
        candidate = candidate[min(starts):]
    return candidate.strip().rstrip(";")


def _is_time_column(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in TIME_MARKERS)


def _is_category_column(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in CATEGORY_MARKERS)


def recommend_chart(columns: list[str], rows: list[list[Any]]) -> ChartSpec:
    if not columns or not rows:
        return ChartSpec(type="table")
    if len(columns) < 2 or len(rows) < 2:
        return ChartSpec(type="table")

    x_field = columns[0]
    y_fields = [columns[1]]
    if _is_time_column(x_field):
        return ChartSpec(type="line", x_field=x_field, y_fields=y_fields,
                         title=f"{x_field} 趋势")
    if _is_category_column(x_field) and len(rows) <= 5:
        return ChartSpec(type="pie", x_field=x_field, y_fields=y_fields,
                         title=f"{x_field} 占比")
    if _is_category_column(x_field):
        return ChartSpec(type="bar", x_field=x_field, y_fields=y_fields,
                         title=f"{x_field} 对比")
    return ChartSpec(type="bar", x_field=x_field, y_fields=y_fields, title="结果对比")


def _format_number(value: Any) -> str:
    if isinstance(value, float):
        if abs(value - round(value)) < 0.005 and abs(value) >= 100:
            return f"{round(value)}"
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return str(value)
    return str(value)


def template_summary(columns: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "没有查询到符合条件的数据。"
    if len(columns) == 1 and len(rows) == 1:
        return f"{columns[0]}为 {_format_number(rows[0][0])}。"
    if len(rows) == 1:
        parts = [f"{column}={_format_number(value)}" for column, value in zip(columns, rows[0], strict=False)]
        return "、".join(parts[:4]) + "。"
    if len(columns) >= 2 and _is_time_column(columns[0]):
        first = _format_number(rows[0][1])
        last = _format_number(rows[-1][1])
        return (f"{columns[0]}从 {rows[0][0]} 到 {rows[-1][0]} 共 {len(rows)} 个数据点，"
                f"{columns[1]} 由 {first} 变为 {last}。")
    top_label = rows[0][0]
    top_value = _format_number(rows[0][1])
    return f"共 {len(rows)} 行，第一行是 {top_label}，{columns[1]} 为 {top_value}。"


def _numbers_in_text(text: str) -> list[float]:
    found: list[float] = []
    for raw in re.findall(r"-?\d[\d,]*(?:\.\d+)?%?", text):
        cleaned = raw.replace(",", "").replace("%", "")
        try:
            found.append(float(cleaned))
        except ValueError:
            continue
    return found


def _candidate_strings(value: Any) -> set[str]:
    candidates: set[str] = set()
    if isinstance(value, (int, float)):
        candidates.add(str(value))
        candidates.add(f"{value:.0f}")
        candidates.add(f"{value:.1f}")
        candidates.add(f"{value:.2f}")
        candidates.add(f"{value:,.0f}")
        candidates.add(f"{value:,.2f}")
        candidates.add(f"{value / 10000:.2f}")
        candidates.add(f"{value / 10000:.1f}")
        candidates.add(f"{value * 100:.2f}")
    text = str(value)
    candidates.add(text)
    for token in re.findall(r"-?\d[\d,]*(?:\.\d+)?", text):
        candidates.add(token.replace(",", ""))
    return candidates


def verify_summary(summary: str, rows: list[list[Any]]) -> bool:
    lowered = summary.lower()
    if len(summary) > 120 or not summary.strip():
        return False
    if any(token in lowered for token in ("select ", " from ", " where ", "sql", "```")):
        return False
    numbers = _numbers_in_text(summary)
    if not numbers:
        return False
    candidates: set[str] = set()
    values: list[float] = []
    for row in rows[:50]:
        for value in row:
            candidates.update(_candidate_strings(value))
            if isinstance(value, (int, float)):
                values.append(float(value))
    if not candidates:
        return False
    for number in numbers:
        if any(number == float(candidate) for candidate in candidates
               if re.fullmatch(r"-?\d+(?:\.\d+)?", candidate)):
            continue
        matched = any(abs(value - number) <= max(0.01, abs(value) * 0.01) for value in values)
        if not matched:
            return False
    return True


def build_metric_notes(bundle: RetrievalBundle, used_tables: set[str]) -> list[MetricNote]:
    notes: list[MetricNote] = []
    for hit in bundle.metrics:
        metric = hit.metric
        related = {part.strip().split(".")[-1] for part in metric.related_tables.split(",")
                   if part.strip()}
        if related and not related & used_tables:
            continue
        notes.append(MetricNote(name=metric.name, definition=metric.definition,
                                formula=metric.formula))
    return notes


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")