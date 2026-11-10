"""What a question asks for, as a structure the program can check.

The router asks the model to fill the structure. This module reads the reply, checks it
against the semantic layer, words the question to ask back when something is missing or
ambiguous, and says in plain words how the question was understood.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..api.schemas import Clarify, HistoryMessage, IntentInfo, RouteChoice
from ..nl2sql.time_parser import TimeRange, parse_comparison, parse_time_range
from ..semantic.models import Metric, SchemaItem

ROUTES = ("query", "analysis", "direct")
SHAPES = ("single", "top_n", "group", "trend", "detail", "compare", "ratio")
GRAINS = ("day", "week", "month", "quarter", "year")
ORDERS = ("desc", "asc")
SHAPE_LABELS = {"single": "一个数值", "group": "分组统计", "trend": "趋势", "detail": "明细",
                "compare": "两期对比", "ratio": "比率"}
GRAIN_LABELS = {"day": "按天", "week": "按周", "month": "按月", "quarter": "按季度",
                "year": "按年"}

TEXT_LIMIT = 100
REASON_LIMIT = 60
LIST_LIMIT = 8
OPTION_LIMIT = 4
# A question is asked back at most this many times; after that the defaults apply.
MAX_CLARIFY_ROUNDS = 2
DEFAULT_TOP_N = 10
# "最近" names no period: the default applies and the answer says so.
VAGUE_TIMES = ("最近", "近期", "近来", "这段时间", "目前", "现在", "当前", "最新")
TIME_OPTIONS = ["最近7天", "最近30天", "上个月", "本月"]
DEFAULT_TIME_NOTE = "未指定时间，按最近 30 天统计"
DEFAULT_COMPARISON_NOTE = "未指定对比期，和上一个周期比"

# The keys of an intent; a reply with none of them only says how to answer.
INTENT_KEYS = ("metrics", "time_range", "comparison", "dimensions", "filters", "result_shape",
               "missing")

IssueKind = Literal["metric_missing", "metric_ambiguous", "filter_value", "time_unparsed",
                    "missing"]


class IntentParseError(ValueError):
    pass


class IntentMetric(BaseModel):
    # The user's wording, a metric of the semantic layer, or how to count it when the
    # semantic layer has none; and the metrics the wording could mean.
    term: str = ""
    name: str = ""
    custom: str = ""
    candidates: list[str] = Field(default_factory=list)


class IntentTime(BaseModel):
    text: str = ""
    # False when the question does not depend on a period ("一共有几个地区").
    needed: bool = True


class IntentDimension(BaseModel):
    term: str = ""
    # "table.column" as the semantic layer names it, without the schema.
    field: str = ""


class IntentFilter(BaseModel):
    term: str = ""
    field: str = ""
    value: str = ""


class ResultShape(BaseModel):
    type: str = ""
    n: int | None = None
    order: str = ""
    grain: str = ""


class Intent(BaseModel):
    route: RouteChoice = "query"
    reason: str = ""
    metrics: list[IntentMetric] = Field(default_factory=list)
    time_range: IntentTime = Field(default_factory=IntentTime)
    comparison: str = ""
    dimensions: list[IntentDimension] = Field(default_factory=list)
    filters: list[IntentFilter] = Field(default_factory=list)
    result_shape: ResultShape = Field(default_factory=ResultShape)
    missing: list[str] = Field(default_factory=list)
    # The model's own question for what is in ``missing``.
    clarify_question: str = ""
    clarify_options: list[str] = Field(default_factory=list)
    # False when the reply named only the route: there is nothing to check.
    structured: bool = Field(default=True, exclude=True)


def _text(value: Any, limit: int = TEXT_LIMIT) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()[:limit]


def _texts(value: Any, limit: int = LIST_LIMIT) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [text for text in (_text(item) for item in value) if text][:limit]


def _items(value: Any) -> list[Any]:
    if isinstance(value, (str, dict)):
        return [value]
    return value[:LIST_LIMIT] if isinstance(value, list) else []


def _metric(value: Any) -> IntentMetric | None:
    if isinstance(value, str):
        value = {"term": value, "name": value}
    if not isinstance(value, dict):
        return None
    metric = IntentMetric(term=_text(value.get("term")), name=_text(value.get("name")),
                          custom=_text(value.get("custom"), 200),
                          candidates=_texts(value.get("candidates"), OPTION_LIMIT))
    return metric if metric.term or metric.name or metric.custom else None


def _dimension(value: Any) -> IntentDimension | None:
    if isinstance(value, str):
        value = {"term": value}
    if not isinstance(value, dict):
        return None
    dimension = IntentDimension(term=_text(value.get("term")),
                                field=_text(value.get("field")).lower())
    return dimension if dimension.term or dimension.field else None


def _filter(value: Any) -> IntentFilter | None:
    if not isinstance(value, dict):
        return None
    item = IntentFilter(term=_text(value.get("term")), field=_text(value.get("field")).lower(),
                        value=_text(value.get("value")))
    return item if item.value or item.term else None


def _shape(value: Any) -> ResultShape:
    if isinstance(value, str):
        value = {"type": value}
    if not isinstance(value, dict):
        return ResultShape()
    kind = _text(value.get("type")).lower()
    order = _text(value.get("order")).lower()
    grain = _text(value.get("grain")).lower()
    count = value.get("n")
    try:
        number = int(count) if count is not None and not isinstance(count, bool) else None
    except (TypeError, ValueError):
        number = None
    return ResultShape(type=kind if kind in SHAPES else "",
                       n=number if number and 0 < number <= 1000 else None,
                       order=order if order in ORDERS else "",
                       grain=grain if grain in GRAINS else "")


def _time(value: Any) -> IntentTime:
    if isinstance(value, str):
        return IntentTime(text=_text(value))
    if not isinstance(value, dict):
        return IntentTime()
    return IntentTime(text=_text(value.get("text")), needed=value.get("needed") is not False)


def intent_from_dict(data: dict[str, Any]) -> Intent:
    """Read an intent leniently: a wrong field is dropped, not an error. The data comes
    from the model, or from the client as the intent of an earlier reply."""
    route = data.get("route")
    clarify = data.get("clarify") if isinstance(data.get("clarify"), dict) else {}
    return Intent(
        route=route if route in ROUTES else "query",
        reason=_text(data.get("reason"), REASON_LIMIT),
        metrics=[item for item in map(_metric, _items(data.get("metrics"))) if item],
        time_range=_time(data.get("time_range")),
        comparison=_text(data.get("comparison")),
        dimensions=[item for item in map(_dimension, _items(data.get("dimensions"))) if item],
        filters=[item for item in map(_filter, _items(data.get("filters"))) if item],
        result_shape=_shape(data.get("result_shape")),
        missing=_texts(data.get("missing"), OPTION_LIMIT),
        clarify_question=_text(clarify.get("question") or data.get("clarify_question"), 200),
        clarify_options=_texts(clarify.get("options") or data.get("clarify_options"),
                               OPTION_LIMIT),
        structured=any(key in data for key in INTENT_KEYS),
    )


def parse_intent(text: str) -> Intent:
    """Read the model's JSON reply; a code fence or text around the object is tolerated.
    The route must be valid: without it the reply cannot be trusted."""
    start = text.find("{")
    if start < 0:
        raise IntentParseError("回复里没有 JSON")
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise IntentParseError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise IntentParseError("JSON 不是对象")
    if data.get("route") not in ROUTES:
        raise IntentParseError(f"route 取值不对：{data.get('route')!r}")
    return intent_from_dict(data)


def previous_intent(history: Iterable[HistoryMessage]) -> Intent | None:
    """The intent of the latest reply that carries one."""
    for message in reversed(list(history)):
        if message.role == "assistant" and message.intent:
            return intent_from_dict(message.intent)
    return None


def clarify_rounds(history: list[HistoryMessage]) -> int:
    """How many times in a row the conversation ended with a question asked back."""
    rounds = 0
    for message in reversed(history):
        if message.role == "user":
            continue
        if message.status != "clarify":
            break
        rounds += 1
    return rounds


def pending_question(history: list[HistoryMessage]) -> str:
    """The question a clarification was asked about, when the last reply asked one."""
    if not history or history[-1].role != "assistant" or history[-1].status != "clarify":
        return ""
    asked = ""
    for index in range(len(history) - 1, -1, -1):
        message = history[index]
        if message.role == "assistant" and message.status != "clarify":
            break
        if message.role == "user":
            asked = message.content
    return asked


@dataclass(slots=True)
class Catalog:
    """What the intent is checked against: the metrics and columns of the semantic
    layer, and the values of the columns users name values of."""

    metrics: list[Metric] = field(default_factory=list)
    schema_items: list[SchemaItem] = field(default_factory=list)
    # Reads the values of an entity column; None when they cannot be read.
    entity_values: Callable[[SchemaItem], list[str] | None] = lambda item: None

    def metric_names(self, wording: str) -> list[str]:
        """The metrics a wording names, by name or alias."""
        wanted = wording.strip().lower()
        if not wanted:
            return []
        found: list[str] = []
        for metric in self.metrics:
            names = [metric.name, *metric.aliases.replace("，", ",").split(",")]
            if wanted in {name.strip().lower() for name in names if name.strip()}:
                found.append(metric.name)
        return found

    def column(self, name: str) -> SchemaItem | None:
        """A column by "table.column"; the schema in front of the table is ignored."""
        parts = name.strip().lower().split(".")
        if len(parts) < 2:
            return None
        table, column = parts[-2], parts[-1]
        for item in self.schema_items:
            if (item.column_name.lower() == column
                    and item.table_name.lower().split(".")[-1] == table):
                return item
        return None


@dataclass(slots=True)
class Issue:
    """Something to ask the user about, and what is assumed when not asking."""

    kind: IssueKind
    question: str
    options: list[str] = field(default_factory=list)
    assumption: str = ""


@dataclass(slots=True)
class Checked:
    intent: Intent
    issues: list[Issue] = field(default_factory=list)
    # What was filled in without asking; shown with the answer.
    assumptions: list[str] = field(default_factory=list)


def _check_metrics(intent: Intent, catalog: Catalog, mode: str) -> tuple[list[IntentMetric],
                                                                        list[Issue]]:
    issues: list[Issue] = []
    metrics: list[IntentMetric] = []
    for metric in intent.metrics:
        by_name = catalog.metric_names(metric.name)
        by_term = catalog.metric_names(metric.term)
        # Ambiguous: the wording is an alias of several metrics, or the model lists several.
        candidates = list(dict.fromkeys([
            *(by_term if len(by_term) > 1 else []),
            *(name for item in metric.candidates for name in catalog.metric_names(item))]))
        if len(candidates) > 1:
            wording = metric.term or metric.name
            pick = by_name[0] if by_name and by_name[0] in candidates else candidates[0]
            issues.append(Issue(
                "metric_ambiguous", f"“{wording}”可以指几个指标，你想看哪一个？",
                candidates[:OPTION_LIMIT], f"“{wording}”按“{pick}”统计"))
            metrics.append(metric.model_copy(update={"name": pick, "candidates": candidates}))
            continue
        names = by_name or by_term or candidates
        if names:
            metrics.append(metric.model_copy(update={"name": names[0], "candidates": []}))
        else:
            # Not in the semantic layer: kept as the model describes it.
            metrics.append(metric.model_copy(update={
                "name": "", "custom": metric.custom or metric.name, "candidates": []}))
    if not metrics and mode == "query" and not intent.missing:
        options = [metric.name for metric in catalog.metrics[:OPTION_LIMIT]]
        issues.append(Issue("metric_missing", "你想看哪个指标？", options))
    return metrics, issues


def match_value(value: str, known: list[str]) -> tuple[str, list[str]]:
    """The stored value a wording names, or the values close to it when none is.

    "抖音渠道" names "抖音". A wording several values contain is left alone: it may mean
    all of them.
    """
    if value in known:
        return value, []
    containing = [item for item in known if item and (value in item or item in value)]
    if len(containing) == 1:
        return containing[0], []
    if containing:
        return value, []
    return "", difflib.get_close_matches(value, known, n=OPTION_LIMIT, cutoff=0.4)


def _check_filters(intent: Intent, catalog: Catalog) -> tuple[list[IntentFilter], list[Issue]]:
    issues: list[Issue] = []
    filters: list[IntentFilter] = []
    for item in intent.filters:
        column = catalog.column(item.field)
        if column is None:
            # A column that does not exist binds nothing; the wording still reaches the agent.
            filters.append(item.model_copy(update={"field": ""}))
            continue
        known = catalog.entity_values(column) if column.is_entity and item.value else None
        if not known:
            filters.append(item)
            continue
        matched, close = match_value(item.value, known)
        if matched:
            filters.append(item.model_copy(update={"value": matched}))
            continue
        label = column.description or column.column_name
        # Nothing close: offer some stored values, so the user sees what there is.
        issues.append(Issue(
            "filter_value", f"没有找到“{item.value}”（{label}），你指的是哪一个？",
            close or known[:OPTION_LIMIT], f"没有找到“{item.value}”，按原话筛选"))
        filters.append(item)
    return filters, issues


def is_vague_time(text: str) -> bool:
    return text.strip() in VAGUE_TIMES


def time_is_parsed(text: str, as_of: date) -> bool:
    return not parse_time_range(text, as_of).is_default


def check_intent(intent: Intent, catalog: Catalog, as_of: date, mode: str = "query") -> Checked:
    """Check an intent against the semantic layer and bring it to the stored names.

    ``mode`` is how the question will be answered. A direct answer needs no check.
    """
    if mode == "direct":
        return Checked(intent)
    metrics, issues = _check_metrics(intent, catalog, mode)
    filters, filter_issues = _check_filters(intent, catalog)
    issues.extend(filter_issues)
    dimensions = [item if catalog.column(item.field) else item.model_copy(update={"field": ""})
                  for item in intent.dimensions]
    assumptions: list[str] = []

    time_range = intent.time_range
    if is_vague_time(time_range.text):
        time_range = IntentTime(text="", needed=True)
    elif time_range.text and not time_is_parsed(time_range.text, as_of):
        issues.append(Issue(
            "time_unparsed", f"没看懂时间“{time_range.text}”，你想看哪个时间段？",
            list(TIME_OPTIONS), f"没看懂时间“{time_range.text}”，按最近 30 天统计"))
    comparison = intent.comparison
    if comparison and not time_is_parsed(comparison, as_of):
        comparison = ""

    shape = intent.result_shape
    if shape.type == "top_n" and shape.n is None:
        shape = shape.model_copy(update={"n": DEFAULT_TOP_N})
        assumptions.append(f"未指定数量，按前 {DEFAULT_TOP_N} 名")

    if intent.missing:
        question = intent.clarify_question or f"还需要确认：{'；'.join(intent.missing)}"
        issues.append(Issue("missing", question, list(intent.clarify_options),
                            f"未确认：{'；'.join(intent.missing)}"))

    checked = intent.model_copy(update={
        "metrics": metrics, "filters": filters, "dimensions": dimensions,
        "time_range": time_range, "comparison": comparison, "result_shape": shape})
    return Checked(checked, issues, assumptions)


def build_clarify(issues: list[Issue], rounds: int) -> Clarify:
    """One question for the user: the first issue, with its options."""
    first = issues[0]
    return Clarify(question=first.question, options=first.options[:OPTION_LIMIT],
                   round=rounds + 1)


def settle(checked: Checked) -> Checked:
    """Stop asking: every open issue takes its default, and the answer says so."""
    intent = checked.intent
    assumptions = [*checked.assumptions,
                   *(issue.assumption for issue in checked.issues if issue.assumption)]
    if any(issue.kind == "time_unparsed" for issue in checked.issues):
        intent = intent.model_copy(update={"time_range": IntentTime()})
    return Checked(intent.model_copy(update={"missing": []}), [], assumptions)


def resolve_time(intent: Intent, parsed: TimeRange, as_of: date) -> tuple[TimeRange, list[str]]:
    """The period to query: the intent's wording read by the time parser, else what the
    parser read from the question and the conversation. Returns the notes to show."""
    notes: list[str] = []
    time_range = parsed
    wording = intent.time_range.text
    if wording:
        named = parse_time_range(wording, as_of)
        if not named.is_default:
            time_range = replace(named, baseline=named.baseline or parsed.baseline)
    if intent.comparison:
        baseline = parse_comparison(intent.comparison, as_of, time_range)
        if baseline is not None:
            time_range = replace(time_range, baseline=baseline)
    if time_range.is_default and intent.time_range.needed:
        notes.append(DEFAULT_TIME_NOTE)
    return time_range, notes


def describe(intent: Intent, time_range: TimeRange) -> str:
    """How the question was understood, in one line for the user."""
    parts: list[str] = []
    if intent.time_range.needed:
        parts.append(f"时间：{time_range.label}")
    if time_range.baseline is not None:
        parts.append(f"对比：{time_range.baseline.label}")
    metrics = [metric.name or metric.custom or metric.term for metric in intent.metrics]
    if metrics:
        parts.append(f"指标：{'、'.join(metrics)}")
    dimensions = [item.term or item.field for item in intent.dimensions]
    if dimensions:
        parts.append(f"按{'、'.join(dimensions)}分组")
    filters = [item.value or item.term for item in intent.filters]
    if filters:
        parts.append(f"筛选：{'、'.join(filters)}")
    shape = intent.result_shape
    if shape.type == "top_n":
        direction = "从低到高" if shape.order == "asc" else "从高到低"
        parts.append(f"结果：前 {shape.n or DEFAULT_TOP_N} 名（{direction}）")
    elif shape.type == "trend":
        parts.append(f"结果：{GRAIN_LABELS.get(shape.grain, '')}趋势")
    elif shape.type in SHAPE_LABELS:
        parts.append(f"结果：{SHAPE_LABELS[shape.type]}")
    return "；".join(parts)


def intent_info(checked: Checked, time_range: TimeRange, notes: list[str]) -> IntentInfo:
    """The intent as the answer carries it: the line for the user and the structure the
    client sends back with the conversation."""
    return IntentInfo(text=describe(checked.intent, time_range),
                      assumptions=[*notes, *checked.assumptions],
                      data=checked.intent.model_dump(exclude_defaults=True))


def agent_note(info: IntentInfo) -> str:
    """The understanding as the query agent reads it."""
    if not info.text:
        return ""
    assumed = f"（{'；'.join(info.assumptions)}）" if info.assumptions else ""
    return f"[系统信息] 对这个问题的理解：{info.text}{assumed}。按这个理解查数。"


__all__ = [
    "MAX_CLARIFY_ROUNDS",
    "Catalog",
    "Checked",
    "Intent",
    "IntentParseError",
    "Issue",
    "agent_note",
    "build_clarify",
    "check_intent",
    "clarify_rounds",
    "describe",
    "intent_from_dict",
    "intent_info",
    "parse_intent",
    "pending_question",
    "previous_intent",
    "resolve_time",
    "settle",
]
