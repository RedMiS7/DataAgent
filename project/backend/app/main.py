from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .agent.pipeline import AskPipeline
from .agent.scope_model import build_chat_model
from .api.routes import router
from .config import get_settings
from .db.engine import business_engine, ping
from .embedding import build_embedder
from .semantic.retriever import SemanticRetriever
from .vector import build_vector_store


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    vector_store = build_vector_store(settings)
    embedder = build_embedder(settings)
    llm = build_chat_model(settings)
    retriever = SemanticRetriever(settings, vector_store, embedder)
    app.state.settings = settings
    app.state.vector_store = vector_store
    app.state.embedder = embedder
    app.state.llm = llm
    app.state.retriever = retriever
    app.state.pipeline = AskPipeline(settings, retriever)
    yield


app = FastAPI(title="DataAgent", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/healthz")
def healthz() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "business_db": ping(business_engine(settings)),
    }