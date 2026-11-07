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


class QueryResult(BaseModel):
    index: int
    purpose: str = ""
    sql: str = ""
    table: TableBlock | None = None
    chart: ChartSpec | None = None


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
    message_id: int | None = None


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    session_id: str = Field(default="default", max_length=64)
    mode: AskMode = "query"
    # Set when re-asking: messages after this id leave the conversation first.
    after_message_id: int | None = Field(default=None, ge=0)


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