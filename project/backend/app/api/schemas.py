"""Request/response models shared by the API and the agent pipeline."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=HISTORY_MAX_CHARS)
    # The mode of an assistant reply, so the router knows how the last turn ran.
    mode: AskMode | None = None


class RouteInfo(BaseModel):
    """How an "auto" question was routed."""

    requested: RequestMode
    chosen: AskMode
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


class AnswerPayload(BaseModel):
    question: str
    rewritten_question: str = ""
    sql: str = ""
    status: Literal["ok", "empty", "refused", "blocked", "error"] = "ok"
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