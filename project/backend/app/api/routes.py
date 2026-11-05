from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from ..agent.pipeline import AskPipeline
from ..analysis import attribute_return_rate
from ..config import Settings
from ..db.engine import app_connection, business_engine, ping
from ..semantic import repository
from .schemas import AnswerPayload, AskRequest, CaseOut, CaseReviewRequest, FeedbackRequest

router = APIRouter(prefix="/api")


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _pipeline(request: Request) -> AskPipeline:
    return request.app.state.pipeline


@router.get("/meta")
def meta(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    store = request.app.state.vector_store
    return {
        "as_of_date": settings.as_of_date.isoformat(),
        "sql_max_rows": settings.sql_max_rows,
        "sql_timeout_ms": settings.sql_timeout_ms,
        "llm_configured": request.app.state.llm is not None,
        "embedding_configured": request.app.state.embedder is not None,
        "qdrant_available": store is not None,
        "business_db_available": ping(business_engine(settings)),
    }


@router.post("/ask", response_model=AnswerPayload)
def ask(request: Request, payload: AskRequest) -> AnswerPayload:
    return _pipeline(request).ask(payload.question, payload.session_id)


@router.post("/ask/stream")
async def ask_stream(request: Request, payload: AskRequest) -> EventSourceResponse:
    pipeline = _pipeline(request)
    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(event: str, data: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (event, data))

    async def produce() -> None:
        try:
            await loop.run_in_executor(None, pipeline.ask, payload.question,
                                       payload.session_id, emit)
        except Exception as exc:
            await queue.put(("error", {"message": f"服务异常：{exc}"}))
        finally:
            await queue.put(("done", {}))

    async def events() -> AsyncIterator[dict[str, str]]:
        task = asyncio.create_task(produce())
        try:
            while True:
                event, data = await queue.get()
                if event == "done":
                    break
                yield {"event": event,
                       "data": json.dumps(data, ensure_ascii=False, default=str)}
        finally:
            await task

    return EventSourceResponse(events())


@router.post("/attribution/return-rate")
def attribution(request: Request, payload: AskRequest) -> dict[str, Any]:
    result = attribute_return_rate(_settings(request), payload.question)
    return {
        "metric": result.metric,
        "current": result.current,
        "previous": result.previous,
        "current_rate": result.current_rate,
        "previous_rate": result.previous_rate,
        "delta_points": result.delta_points,
        "total_current_refund": result.total_current_refund,
        "total_previous_refund": result.total_previous_refund,
        "summary": result.summary,
        "summary_source": result.summary_source,
        "factors": [
            {"dimension": factor.dimension, "name": factor.name,
             "current_refund": factor.current_refund, "previous_refund": factor.previous_refund,
             "delta": factor.delta, "share": factor.share}
            for factor in result.factors
        ],
        "sql": result.sql,
    }


@router.post("/feedback")
def feedback(request: Request, payload: FeedbackRequest) -> dict[str, str]:
    with app_connection(_settings(request)) as conn:
        repository.insert_feedback(conn, payload.log_id, payload.verdict)
    return {"status": "ok"}


@router.get("/cases", response_model=list[CaseOut])
def cases(request: Request, status: str | None = Query(default=None)) -> list[CaseOut]:
    with app_connection(_settings(request)) as conn:
        rows = repository.list_cases(conn, status)
    return [CaseOut(**row) for row in rows]


@router.post("/cases/{case_id}/review")
def review(request: Request, case_id: int, payload: CaseReviewRequest) -> dict[str, str]:
    with app_connection(_settings(request)) as conn:
        updated = repository.review_case(conn, case_id, payload.status)
    if not updated:
        raise HTTPException(status_code=404, detail="案例不存在")
    return {"status": payload.status}