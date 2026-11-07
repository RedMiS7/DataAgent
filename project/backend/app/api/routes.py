from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from ..agent.pipeline import AskPipeline
from ..agent.scope_model import build_chat_model
from ..agent.session import log_tag
from ..analysis import attribute_return_rate
from ..bootstrap import Bootstrapper, KeyValidationError, validate_keys
from ..config import SAVED_KEY_FIELDS, Settings, save_keys
from ..db.engine import app_connection, business_engine, ping
from ..embedding import build_embedder
from ..semantic import repository
from .schemas import (
    AnswerPayload,
    AskRequest,
    CaseOut,
    CaseReviewRequest,
    FeedbackRequest,
    SaveKeysRequest,
    SetupStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _pipeline(request: Request) -> AskPipeline:
    return request.app.state.pipeline


def _bootstrapper(request: Request) -> Bootstrapper:
    return request.app.state.bootstrapper


@router.get("/setup/status", response_model=SetupStatus)
def setup_status(request: Request) -> SetupStatus:
    return _bootstrapper(request).status()


@router.put("/setup/keys", response_model=SetupStatus)
def setup_keys(request: Request, payload: SaveKeysRequest) -> SetupStatus:
    """Check and save keys. They are write-only: no endpoint returns them."""
    settings = _settings(request)
    llm_key = payload.llm_api_key.strip()
    embedding_key = payload.embedding_api_key.strip()
    if not llm_key and not embedding_key:
        raise HTTPException(status_code=400, detail="请至少填写一个 Key")
    try:
        validate_keys(settings, llm_key, embedding_key)
    except KeyValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    entered = {"LLM_API_KEY": llm_key, "EMBEDDING_API_KEY": embedding_key}
    save_keys(settings, {name: entered[name] for name in SAVED_KEY_FIELDS if entered[name]})

    state = request.app.state
    state.llm = build_chat_model(settings)
    state.embedder = build_embedder(settings)
    state.retriever.embedder = state.embedder
    bootstrapper = _bootstrapper(request)
    bootstrapper.start()
    return bootstrapper.status()


@router.post("/setup/retry", response_model=SetupStatus)
def setup_retry(request: Request) -> SetupStatus:
    bootstrapper = _bootstrapper(request)
    bootstrapper.start()
    return bootstrapper.status()


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
    return _pipeline(request).ask(payload.question, payload.session_id, mode=payload.mode,
                                  history=payload.history)


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
                                       payload.session_id, emit, payload.mode,
                                       payload.history)
        except Exception as exc:
            logger.exception("[%s] 问答异常", log_tag(payload.session_id))
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