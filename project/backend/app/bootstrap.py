"""First-run setup: runs in the background so nothing needs a separate command."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from . import demo_data
from .agent import session
from .agent.scope_model import build_chat_model, probe_chat_model
from .api.schemas import SetupStatus, SetupStep
from .config import Settings
from .db.engine import (
    DatabaseAccessDenied,
    app_connection,
    app_engine,
    business_connection,
    ping,
)
from .embedding import build_embedder
from .embedding.base import EmbeddingProvider
from .semantic import indexer, repository
from .semantic.retriever import SemanticRetriever
from .vector import build_vector_store
from .vector.base import VectorStore

logger = logging.getLogger(__name__)

STEPS = (
    ("database", "连接数据库"),
    ("semantic", "同步业务口径"),
    ("demo_data", "准备模拟数据"),
    ("index", "建立检索索引"),
)
FINISHED_STATES = {"done", "skipped"}
DATABASE_WAIT_SECONDS = 120
VECTOR_WAIT_SECONDS = 20


class KeyValidationError(ValueError):
    pass


def _scrub(error: Exception, *secrets: str) -> str:
    text = str(error)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text[:300]


def validate_keys(settings: Settings, llm_key: str, embedding_key: str) -> None:
    """Call each provider once so a bad key fails here, not on the first question."""
    if llm_key:
        candidate = settings.model_copy(update={"llm_api_key": llm_key})
        model = build_chat_model(candidate)
        if model is None:
            raise KeyValidationError("大模型配置不完整，请检查 LLM_PROVIDER")
        try:
            probe_chat_model(model)
        except Exception as exc:
            raise KeyValidationError(f"大模型 Key 验证失败：{_scrub(exc, llm_key)}") from exc
    if embedding_key:
        candidate = settings.model_copy(update={"embedding_api_key": embedding_key})
        embedder = build_embedder(candidate)
        if embedder is None:
            raise KeyValidationError("向量模型配置不完整，请检查 EMBEDDING_PROVIDER")
        try:
            embedder.embed(["连接检查"])
        except Exception as exc:
            raise KeyValidationError(f"向量模型 Key 验证失败：{_scrub(exc, embedding_key)}") from exc


def index_is_current(settings: Settings, store: VectorStore, embedder: EmbeddingProvider) -> bool:
    """Vectors from another embedding model cannot be searched, and an index of an older
    version lacks what retrieval now reads, so both count as stale."""
    collection = settings.collection_name("schema_items")
    if store.count(collection) == 0:
        return False
    hits = store.search(collection, embedder.embed(["索引检查"])[0], 1)
    return (bool(hits) and hits[0].payload.get("model") == embedder.model_name
            and hits[0].payload.get("index_version") == indexer.INDEX_VERSION)


def _wait_for(check: Callable[[], bool], seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)
    return True


class Bootstrapper:
    """Runs idempotent setup steps; a failed or waiting run can simply start again."""

    def __init__(self, settings: Settings, retriever: SemanticRetriever) -> None:
        self.settings = settings
        self.retriever = retriever
        self._lock = threading.Lock()
        self._running = False
        self._run_again = False
        self._steps = [SetupStep(key=key, label=label) for key, label in STEPS]
        self._error = ""
        self._orders_done = 0
        self._orders_total = 0

    def start(self) -> None:
        with self._lock:
            if self._running:
                self._run_again = True
                return
            self._running = True
        threading.Thread(target=self._run, name="bootstrap", daemon=True).start()

    def status(self) -> SetupStatus:
        with self._lock:
            steps = [step.model_copy() for step in self._steps]
            error = self._error
            orders_done = self._orders_done
            orders_total = self._orders_total
        llm_configured = build_chat_model(self.settings) is not None
        return SetupStatus(
            ready=llm_configured and all(step.state in FINISHED_STATES for step in steps),
            llm_configured=llm_configured,
            embedding_configured=self.retriever.embedder is not None,
            steps=steps,
            orders_done=orders_done,
            orders_total=orders_total,
            error=error,
        )

    def _run(self) -> None:
        while True:
            self._run_steps()
            with self._lock:
                if not self._run_again:
                    self._running = False
                    return
                self._run_again = False

    def _run_steps(self) -> None:
        actions = {
            "database": self._connect_database,
            "semantic": self._sync_semantic,
            "demo_data": self._prepare_demo_data,
            "index": self._prepare_index,
        }
        with self._lock:
            self._error = ""
            for step in self._steps:
                step.state, step.detail = "pending", ""
        for key, _ in STEPS:
            self._set_step(key, "running")
            try:
                state, detail = actions[key]()
            except Exception as exc:
                logger.exception("setup step %s failed", key)
                self._set_step(key, "failed")
                with self._lock:
                    self._error = _scrub(exc, self.settings.llm_api_key,
                                         self.settings.embedding_api_key)
                return
            self._set_step(key, state, detail)

    def _set_step(self, key: str, state: str, detail: str = "") -> None:
        with self._lock:
            step = next(item for item in self._steps if item.key == key)
            step.state, step.detail = state, detail  # type: ignore[assignment]

    def _connect_database(self) -> tuple[str, str]:
        engine = app_engine(self.settings)
        try:
            reachable = _wait_for(lambda: ping(engine, raise_denied=True), DATABASE_WAIT_SECONDS)
        except DatabaseAccessDenied as exc:
            raise RuntimeError(
                f"应用账号 {self.settings.mysql_app_user} 登录不了应用库 "
                f"{self.settings.mysql_app_db}：{exc}。已有的数据库要先用管理账号建好这个账号："
                "python -m app.semantic.cli grant_app_user") from exc
        if not reachable:
            raise RuntimeError(f"连不上 MySQL：{self.settings.mysql_host}:{self.settings.mysql_port}")
        # Databases created before call tracing lack this table.
        with app_connection(self.settings) as conn:
            session.ensure_trace_table(conn)
            repository.ensure_entity_column(conn)
        return "done", ""

    def _sync_semantic(self) -> tuple[str, str]:
        bundle = repository.load_seed(self.settings.seed_dir)
        with app_connection(self.settings) as conn:
            repository.sync_seed(conn, bundle)
        return "done", ""

    def _prepare_demo_data(self) -> tuple[str, str]:
        if not self.settings.generate_demo_data:
            return "skipped", "这个数据集不生成演示数据"
        if demo_data.has_orders(self.settings):
            return "done", "已有数据，保持不变"
        with self._lock:
            self._orders_done, self._orders_total = 0, demo_data.DEFAULT_ORDERS
        demo_data.generate(self.settings, self._record_orders)
        return "done", ""

    def _record_orders(self, count: int) -> None:
        with self._lock:
            self._orders_done = count

    def _prepare_index(self) -> tuple[str, str]:
        embedder = self.retriever.embedder
        if embedder is None:
            return "waiting", "填写向量模型 Key 后继续"
        if embedder.model_name.startswith("fake"):
            return "skipped", "离线模式使用关键词检索"

        def connect() -> bool:
            self.retriever.store = self.retriever.store or build_vector_store(self.settings)
            return self.retriever.store is not None

        if not _wait_for(connect, VECTOR_WAIT_SECONDS):
            return "skipped", "向量库不可用，使用关键词检索"
        store = self.retriever.store
        assert store is not None
        if index_is_current(self.settings, store, embedder):
            return "done", "索引已是最新"

        with app_connection(self.settings) as app_conn, \
                business_connection(self.settings) as business_conn:
            indexer.build_index(self.settings, app_conn, business_conn, store, embedder)
        return "done", ""
