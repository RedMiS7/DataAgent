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


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    session_id: str = Field(default="default", max_length=64)


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