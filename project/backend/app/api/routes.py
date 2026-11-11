from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..agent.pipeline import AskPipeline
from ..agent.scope_model import build_chat_model
from ..agent.session import log_tag
from ..bootstrap import Bootstrapper, KeyValidationError, validate_keys
from ..cache import limit_exceeded, rate_limit_store, read_stats, shared_redis
from ..config import SAVED_KEY_FIELDS, Settings, save_keys
from ..datasets import SuggestedQuestion
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


def _check_rate_limit(request: Request, session_id: str) -> None:
    """Questions call the model, so each session and each client IP gets a quota."""
    settings = _settings(request)
    client_ip = request.client.host if request.client else ""
    wait = limit_exceeded(rate_limit_store(settings), settings.mysql_business_db,
                          settings.rate_limit_per_minute,
                          {"session": session_id, "ip": client_ip})
    if wait is not None:
        logger.warning("[%s] 提问过于频繁，已拒绝 ip=%s", log_tag(session_id), client_ip)
        raise HTTPException(
            status_code=429,
            detail=f"提问太频繁：每分钟最多 {settings.rate_limit_per_minute} 个问题，"
                   f"请 {wait} 秒后再试",
            headers={"Retry-After": str(wait)})


class CacheCounts(BaseModel):
    hit: int
    miss: int


class MetaResponse(BaseModel):
    as_of_date: date
    suggested_questions: list[SuggestedQuestion]
    sql_max_rows: int
    sql_timeout_ms: int
    llm_configured: bool
    embedding_configured: bool
    qdrant_available: bool
    business_db_available: bool
    redis_available: bool
    cache_enabled: bool
    # Hits and misses of the sql, emb and rerank caches since Redis started.
    cache_stats: dict[str, CacheCounts]


@router.get("/meta")
def meta(request: Request) -> MetaResponse:
    settings = _settings(request)
    dataset = settings.dataset
    redis_client = shared_redis(settings)
    redis_available = redis_client is not None and redis_client.ping()
    stats = read_stats(redis_client, settings.mysql_business_db) if redis_available else {}
    return MetaResponse(
        as_of_date=settings.as_of_date,
        suggested_questions=dataset.suggested_questions if dataset else [],
        sql_max_rows=settings.sql_max_rows,
        sql_timeout_ms=settings.sql_timeout_ms,
        llm_configured=request.app.state.llm is not None,
        embedding_configured=request.app.state.embedder is not None,
        qdrant_available=request.app.state.vector_store is not None,
        business_db_available=ping(business_engine(settings)),
        redis_available=redis_available,
        cache_enabled=settings.cache_enabled and redis_available,
        cache_stats={kind: CacheCounts(**counts) for kind, counts in stats.items()},
    )


@router.post("/ask", response_model=AnswerPayload)
def ask(request: Request, payload: AskRequest) -> AnswerPayload:
    _check_rate_limit(request, payload.session_id)
    return _pipeline(request).ask(payload.question, payload.session_id, mode=payload.mode,
                                  history=payload.history)


@router.post("/ask/stream")
async def ask_stream(request: Request, payload: AskRequest) -> EventSourceResponse:
    # Before the stream opens, so the client gets a plain 429.
    _check_rate_limit(request, payload.session_id)
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
