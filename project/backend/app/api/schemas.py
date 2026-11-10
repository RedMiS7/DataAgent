"""Request/response models shared by the API and the agent pipeline."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator


class TableBlock(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool = False
    row_count: int = 0


class ChartSpec(BaseModel):
    type: Literal["line", "bar", "pie", "table"] = "table"
    x_field: str = ""
    y_fields: list[str] = Field(default_factory=list)
    title: str = ""


class MetricNote(BaseModel):
    name: str
    definition: str
    formula: str = ""


AskMode = Literal["query", "analysis"]
# "auto" lets the router pick a mode; answers always carry the mode that ran.
RequestMode = Literal["auto", "query", "analysis"]

HISTORY_MAX_MESSAGES = 200
HISTORY_MAX_CHARS = 4000
# An analysis answer shows at most 10 queries.
HISTORY_MAX_QUERIES = 10
# The intent of a reply, as JSON text.
HISTORY_MAX_INTENT_CHARS = 4000


# How an earlier reply ended; "stopped" means the user stopped it before an answer,
# "clarify" that the reply asked the user a question instead of answering.
HistoryStatus = Literal["ok", "empty", "partial", "text", "refused", "blocked", "error",
                        "stopped", "clarify"]


class HistoryQuery(BaseModel):
    """A query an earlier reply rests on: what it was for and its SQL."""

    purpose: str = Field(default="", max_length=200)
    sql: str = Field(max_length=HISTORY_MAX_CHARS)


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=HISTORY_MAX_CHARS)
    # The mode of an assistant reply, so the router knows how the last turn ran.
    mode: AskMode | None = None
    # Failed and stopped replies are sent too, so every question has a reply.
    status: HistoryStatus | None = None
    # The queries of a reply, so a follow-up can reuse how the data was counted. They
    # come from the client: the model reads them as reference only, nothing runs them.
    queries: list[HistoryQuery] = Field(default_factory=list, max_length=HISTORY_MAX_QUERIES)
    # The period the reply covered, as AnswerPayload.time_range gives it.
    time_range: dict[str, Annotated[str, Field(max_length=64)]] = Field(default_factory=dict,
                                                                         max_length=4)
    # How the reply understood its question, as AnswerPayload.intent.data gives it. A
    # follow-up or an answer to a clarification builds on it. It comes from the client:
    # the model reads it as reference, and the result is checked again.
    intent: dict[str, Any] | None = None

    @field_validator("intent")
    @classmethod
    def _intent_fits(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and len(json.dumps(value, ensure_ascii=False)) > \
                HISTORY_MAX_INTENT_CHARS:
            raise ValueError("intent 太长")
        return value


# "direct": answered from the conversation and the metric definitions, without SQL.
RouteChoice = Literal["query", "analysis", "direct"]


class RouteInfo(BaseModel):
    """How an "auto" question was routed."""

    requested: RequestMode
    chosen: RouteChoice
    reason: str = ""
    source: Literal["llm", "fallback"]


class QueryResult(BaseModel):
    index: int
    purpose: str = ""
    sql: str = ""
    table: TableBlock | None = None
    chart: ChartSpec | None = None
    # The plan step that ran this query; None when analysis ran without a plan.
    step_id: int | None = None


class PlanStepInfo(BaseModel):
    id: int
    goal: str
    origin: Literal["plan", "supplement"] = "plan"
    status: Literal["pending", "running", "done", "failed"]


class CallUsage(BaseModel):
    """Model and tool calls behind one answer."""

    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Part of input_tokens read from the provider's prompt cache, billed at a lower price.
    cached_input_tokens: int = 0
    # Summed over calls; steps running at once make it exceed the answer's duration.
    model_ms: int = 0
    # Writing the answer's log, call and query records at the end.
    log_write_ms: int = 0


# "partial": some queries ran but the answer is incomplete; "error": no query result at all;
# "text": answered without querying, e.g. a metric definition; "clarify": the question
# is ambiguous or lacks something, and the reply asks the user instead of answering.
AnswerStatus = Literal["ok", "empty", "partial", "text", "refused", "blocked", "error",
                       "clarify"]
FailureKind = Literal["timeout", "sql_failed", "model_error", "no_answer", "step_failed",
                      "unexpected"]


class Failure(BaseModel):
    """What went wrong in a partial or failed answer, in words for the user."""

    kind: FailureKind
    message: str
    suggestion: str = ""


class Clarify(BaseModel):
    """A question asked back to the user, with answers to pick from."""

    question: str
    options: list[str] = Field(default_factory=list)
    # 1 for the first question asked about a user question, 2 for the second.
    round: int = 1


class IntentInfo(BaseModel):
    """How the question was understood."""

    # One line for the user: "时间：上个月；指标：销售额；按地区分组".
    text: str = ""
    # What was filled in without asking, e.g. the default period.
    assumptions: list[str] = Field(default_factory=list)
    # The structure; the client sends it back with the conversation.
    data: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    """The query rows and columns a point rests on (plan 2.4)."""

    # The query's number, as QueryResult.index and the [查询n] marks give it.
    query: int
    # Row numbers from 0; empty means every row. Columns by name; empty means every column.
    rows: list[int] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    # The cited cells, cut from the query result by the program, not written by the model.
    table: TableBlock | None = None


class Point(BaseModel):
    claim: str
    evidence: list[Evidence] = Field(default_factory=list)


class Conclusion(BaseModel):
    """The model's conclusion as a structure: what the text of ``summary`` is made from."""

    judgement: str = ""
    points: list[Point] = Field(default_factory=list)
    # Analysis only.
    advice: str = ""
    # False when some evidence was still wrong after one rewrite: the wrong evidence is
    # left out, and the answer shows every query instead of the cited ones.
    checked: bool = True


class AnswerPayload(BaseModel):
    question: str
    rewritten_question: str = ""
    sql: str = ""
    status: AnswerStatus = "ok"
    failure: Failure | None = None
    table: TableBlock | None = None
    chart: ChartSpec | None = None
    summary: str = ""
    summary_source: Literal["model", "template"] = "template"
    metrics: list[MetricNote] = Field(default_factory=list)
    time_range: dict[str, str] = Field(default_factory=dict)
    tables_used: list[str] = Field(default_factory=list)
    retrieval: dict[str, Any] = Field(default_factory=dict)
    retries: int = 0
    log_id: int | None = None
    degraded: bool = False
    warnings: list[str] = Field(default_factory=list)
    mode: AskMode = "query"
    results: list[QueryResult] = Field(default_factory=list)
    route: RouteInfo | None = None
    # The steps of a planned analysis; empty otherwise.
    plan: list[PlanStepInfo] = Field(default_factory=list)
    usage: CallUsage | None = None
    # None when the intent could not be read, and for refusals and direct answers.
    intent: IntentInfo | None = None
    # Set when status is "clarify".
    clarify: Clarify | None = None
    # None when the model wrote no readable conclusion, or none was needed.
    conclusion: Conclusion | None = None


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    session_id: str = Field(default="default", max_length=64)
    mode: RequestMode = "query"
    # The conversation so far, oldest first; the client keeps it, the server stores none.
    history: list[HistoryMessage] = Field(default_factory=list,
                                          max_length=HISTORY_MAX_MESSAGES)


class SetupStep(BaseModel):
    key: str
    label: str
    state: Literal["pending", "running", "done", "waiting", "skipped", "failed"] = "pending"
    detail: str = ""


class SetupStatus(BaseModel):
    ready: bool = False
    llm_configured: bool = False
    embedding_configured: bool = False
    steps: list[SetupStep] = Field(default_factory=list)
    orders_done: int = 0
    orders_total: int = 0
    error: str = ""


class SaveKeysRequest(BaseModel):
    llm_api_key: str = Field(default="", max_length=512)
    embedding_api_key: str = Field(default="", max_length=512)


class FeedbackRequest(BaseModel):
    log_id: int
    verdict: Literal["correct", "wrong"]


class CaseReviewRequest(BaseModel):
    status: Literal["approved", "rejected"]


class CaseOut(BaseModel):
    id: int
    question: str
    sql_text: str
    result_summary: str = ""
    status: str
    created_at: str = ""