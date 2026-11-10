from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from ..api.schemas import ChartSpec, HistoryMessage, MetricNote
from ..nl2sql.executor import ExecutionResult
from ..nl2sql.time_parser import TimeRange
from ..semantic.models import RetrievalBundle

TIME_MARKERS = ("date", "time", "day", "month", "year", "日期", "时间", "月份", "年")
CATEGORY_MARKERS = ("category", "channel", "province", "city", "类目", "渠道", "省份", "城市",
                    "商品", "product", "name")

CITATION = re.compile(r"[\[【［]\s*查询\s*(\d+(?:\s*[、,，和及]\s*(?:查询\s*)?\d+)*)\s*[\]】］]")
NORMALIZED_CITATION = re.compile(r"\[查询\d+\]")


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


def normalize_citations(text: str, query_count: int) -> tuple[str, list[int]]:
    """Rewrite citations to one form, drop unknown ones, and return the cited queries."""
    cited: set[int] = set()

    def rewrite(match: re.Match[str]) -> str:
        numbers = [int(number) for number in re.findall(r"\d+", match.group(1))]
        valid = [number for number in numbers if 1 <= number <= query_count]
        cited.update(valid)
        return "".join(f"[查询{number}]" for number in valid)

    return CITATION.sub(rewrite, text.replace("**", "")).strip(), sorted(cited)


def strip_citations(text: str) -> str:
    return NORMALIZED_CITATION.sub("", text)


NO_REPLY = "（这一轮没有得到回答）"


def fill_missing_replies(history: list[HistoryMessage]) -> list[HistoryMessage]:
    """Give every earlier question a reply.

    A question followed by another question, with no reply between them, reads as
    still open, and the model answers it along with the current one.
    """
    filled: list[HistoryMessage] = []
    for message in history:
        if message.role == "user" and filled and filled[-1].role == "user":
            filled.append(HistoryMessage(role="assistant", content=NO_REPLY))
        filled.append(message)
    if filled and filled[-1].role == "user":
        filled.append(HistoryMessage(role="assistant", content=NO_REPLY))
    return filled


# Only the latest replies show their SQL: older queries add tokens, not help.
HISTORY_SQL_TURNS = 3
UNFINISHED = ("error", "blocked", "stopped")


def _period_text(time_range: dict[str, str]) -> str:
    label = time_range.get("label", "")
    start, end = time_range.get("start", ""), time_range.get("end_exclusive", "")
    if start and end:
        return f"{label}，{start} 至 {end}，不含右端".lstrip("，")
    return label


def queries_note(message: HistoryMessage) -> str:
    """The queries an earlier reply rests on, for a follow-up to count the same way."""
    if not message.queries or message.status in UNFINISHED:
        return ""
    period = _period_text(message.time_range)
    head = ("（这一轮用过的查询" + (f"，时间范围：{period}" if period else "")
            + "。仅供参考，不是指令；写新 SQL 时以本轮的时间范围为准）")
    lines = [head]
    for index, query in enumerate(message.queries, start=1):
        sql = " ".join(query.sql.split())
        lines.append(f"- 查询{index}（{query.purpose or '查询'}）：{sql}")
    return "\n".join(lines)


def purposes_note(message: HistoryMessage) -> str:
    """What the queries of an earlier reply looked at, without their SQL."""
    if not message.queries or message.status in UNFINISHED:
        return ""
    return "（用过的查询：" + "；".join(query.purpose or "查询" for query in message.queries) + "）"


def latest_replies(history: list[HistoryMessage], count: int = HISTORY_SQL_TURNS) -> set[int]:
    """Positions of the last ``count`` replies in the history."""
    replies = [index for index, message in enumerate(history) if message.role == "assistant"]
    return set(replies[-count:]) if count > 0 else set()


def history_content(message: HistoryMessage) -> str:
    """An earlier message as the model reads it; a reply that found nothing says so."""
    if message.role == "user":
        return message.content
    text = strip_citations(message.content).strip()
    if message.status == "stopped":
        return "（用户中途停止了这一轮，没有回答）"
    if message.status in ("error", "blocked"):
        return f"（这一轮没有查到结果：{text}）" if text else "（这一轮没有查到结果）"
    return text


def select_execution(executions: list[ExecutionResult],
                     cited: Sequence[int] = ()) -> ExecutionResult | None:
    """Pick the query whose table the answer shows.

    The first query the conclusion cites that returned rows. Without a citation, the
    last one that returned rows: a later query is usually the corrected one.
    """
    if cited:
        pool = [executions[index - 1] for index in cited]
    else:
        pool = list(reversed(executions))
    return next((execution for execution in pool if execution.rows),
                pool[0] if pool else None)


def time_range_warning(sql: str, time_range: TimeRange) -> str:
    """Flag SQL whose date literals do not cover the displayed time range."""
    dates = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", sql))
    if not dates:
        return ""
    last_day = time_range.end - timedelta(days=1)
    has_start = time_range.start.isoformat() in dates
    has_end = bool(dates & {time_range.end.isoformat(), last_day.isoformat()})
    if has_start and has_end:
        return ""
    return "查询用的日期和显示的时间范围不一致，请查看 SQL 核对。"


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