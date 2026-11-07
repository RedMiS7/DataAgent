from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .agent.pipeline import AskPipeline
from .agent.scope_model import build_chat_model
from .api.routes import router
from .bootstrap import Bootstrapper
from .config import BACKEND_DIR, get_settings
from .db.engine import business_engine, ping
from .embedding import build_embedder
from .semantic.retriever import SemanticRetriever
from .vector import build_vector_store

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class ClosedClientFilter(logging.Filter):
    """Hide the model client's cleanup error.

    Each answer ends its own event loop, and the HTTP client closes after that.
    Answers are unaffected, but the traceback buries the useful lines.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "AsyncClient.aclose" not in record.getMessage()


def configure_logging(level: str) -> None:
    # Third-party loggers stay at WARNING; only this app's own lines follow LOG_LEVEL.
    logging.basicConfig(level=logging.WARNING, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    logging.getLogger("app").setLevel(level.upper())
    logging.getLogger("asyncio").addFilter(ClosedClientFilter())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
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
    app.state.bootstrapper = Bootstrapper(settings, retriever)
    app.state.bootstrapper.start()
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


# The image bundles the built frontend here. Keep this last: "/" would shadow later routes.
STATIC_DIR = BACKEND_DIR / "static"
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")
