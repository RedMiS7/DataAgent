from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from typing import Any

import fakeredis
import pytest
import redis
from app import cache as cache_module
from app.agent import tools
from app.api.routes import router
from app.api.schemas import AnswerPayload
from app.cache import MemoryCache, RedisCache, limit_exceeded, read_stats
from app.cache.base import Cache
from app.config import Settings
from app.embedding.cached import CachedEmbedder
from app.nl2sql import executor
from app.nl2sql.executor import SQLExecutionError, execute_sql
from app.rerank.base import RerankResult
from app.rerank.cached import CachedReranker
from app.safety.validator import SafeSQL
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError


def _settings(**fields: Any) -> Settings:
    base: dict[str, Any] = {"as_of_date_override": date(2026, 10, 31)}
    return Settings(_env_file=None).model_copy(update=base | fields)


@pytest.fixture(params=["memory", "redis"])
def cache(request: pytest.FixtureRequest) -> Cache:
    if request.param == "memory":
        return MemoryCache()
    return RedisCache(fakeredis.FakeRedis())


# --- the two caches behave the same ---


def test_read_write_and_batch(cache: Cache) -> None:
    assert cache.get_many(["a", "b"]) == [None, None]
    cache.set_many({"a": b"1", "b": b"2"}, 60)
    assert cache.get_many(["b", "missing", "a"]) == [b"2", None, b"1"]


def test_count_starts_its_expiry_once(cache: Cache) -> None:
    assert cache.incr("n", 1, 60) == 1
    assert cache.incr("n", 2, 60) == 3
    assert cache.get_many(["n"]) == [b"3"]


def test_clear_drops_only_the_prefix(cache: Cache) -> None:
    cache.set_many({"da:v1:sql:x": b"1", "da:v1:sql:y": b"2", "da:v1:rl:z": b"3"}, 60)
    assert cache.clear("da:v1:sql:") == 2
    assert cache.get_many(["da:v1:sql:x", "da:v1:rl:z"]) == [None, b"3"]


def test_memory_cache_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr("app.cache.memory_cache.time.monotonic", lambda: now[0])
    memory = MemoryCache()
    memory.set_many({"a": b"1"}, 10)
    memory.incr("n", 1, 10)
    now[0] += 11
    assert memory.get_many(["a", "n"]) == [None, None]


def test_redis_cache_sets_the_ttl() -> None:
    client = fakeredis.FakeRedis()
    redis_cache = RedisCache(client)
    redis_cache.set_many({"a": b"1"}, 100)
    redis_cache.incr("n", 1, 60)
    redis_cache.incr("n", 1, 60)
    assert 0 < client.ttl("a") <= 100
    assert 0 < client.ttl("n") <= 60


class BrokenRedis:
    """Every call fails, as when Redis is down."""

    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        def fail(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            raise redis.ConnectionError("connection refused")
        return fail


def test_redis_failures_are_misses_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    client = BrokenRedis()
    broken = RedisCache(client)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="app.cache.redis_cache"):
        assert broken.get_many(["a"]) == [None]
    assert "Redis 读取失败" in caplog.text
    # After one failure Redis is skipped for a while instead of waiting on every call.
    broken.set_many({"a": b"1"}, 60)
    assert broken.incr("n") is None
    assert broken.ping() is False
    assert client.calls == 2  # the read, then the ping


def test_shared_redis_needs_a_url() -> None:
    assert cache_module.shared_redis(_settings(redis_url="")) is None
    assert cache_module.caching(_settings(redis_url="redis://127.0.0.1:6399/0",
                                          cache_enabled=False)) is None
    # Without Redis, rate limits are counted in this process.
    assert isinstance(cache_module.rate_limit_store(_settings(redis_url="")), MemoryCache)


# --- SQL results ---


class FakeResult:
    def __init__(self, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
        self.columns = columns
        self.rows = rows

    def keys(self) -> list[str]:
        return self.columns

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        return self.rows[:size]


class FakeDatabase:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None, fail: bool = False) -> None:
        self.rows = rows if rows is not None else [(1, "华东")]
        self.fail = fail
        self.queries: list[str] = []

    @contextmanager
    def connect(self, settings: Settings) -> Iterator[Any]:
        database = self

        class Conn:
            def execute(self, statement: Any) -> FakeResult:
                database.queries.append(str(statement))
                if database.fail:
                    raise OperationalError("SELECT", {}, Exception("gone"))
                return FakeResult(["n", "region"], database.rows)

        yield Conn()


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    fake = FakeDatabase()
    monkeypatch.setattr(executor, "business_connection", fake.connect)
    return fake


@pytest.fixture
def sql_cache(monkeypatch: pytest.MonkeyPatch) -> MemoryCache:
    memory = MemoryCache()
    monkeypatch.setattr(executor, "caching", lambda settings: memory)
    return memory


SQL = "SELECT COUNT(*) AS n, region FROM safe.orders GROUP BY region LIMIT 500"


def test_the_same_sql_runs_once(database: FakeDatabase, sql_cache: MemoryCache) -> None:
    settings = _settings()
    first = execute_sql(settings, SafeSQL(SQL))
    # Whitespace does not make it a different statement.
    second = execute_sql(settings, SafeSQL(SQL.replace(" ", "  ")))
    assert len(database.queries) == 1
    assert not first.cached and second.cached
    assert (second.columns, second.rows, second.truncated) == (
        first.columns, first.rows, first.truncated)
    assert read_stats(sql_cache, settings.mysql_business_db)["sql"] == {"hit": 1, "miss": 1}


@pytest.mark.parametrize("change", [{"as_of_date_override": date(2026, 11, 30)},
                                    {"sql_max_rows": 10},
                                    {"mysql_business_db": "dabench"}])
def test_a_different_date_limit_or_database_misses(database: FakeDatabase,
                                                   sql_cache: MemoryCache,
                                                   change: dict[str, Any]) -> None:
    execute_sql(_settings(), SafeSQL(SQL))
    assert not execute_sql(_settings(**change), SafeSQL(SQL)).cached
    assert len(database.queries) == 2


def test_a_failed_query_is_not_cached(database: FakeDatabase, sql_cache: MemoryCache) -> None:
    database.fail = True
    for _ in range(2):
        with pytest.raises(SQLExecutionError):
            execute_sql(_settings(), SafeSQL(SQL))
    assert len(database.queries) == 2


def test_a_large_result_is_not_cached(database: FakeDatabase, sql_cache: MemoryCache,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "MAX_CACHED_BYTES", 10)
    execute_sql(_settings(), SafeSQL(SQL))
    assert not execute_sql(_settings(), SafeSQL(SQL)).cached


def test_without_a_cache_every_query_runs(database: FakeDatabase) -> None:
    for _ in range(2):
        execute_sql(_settings(redis_url=""), SafeSQL(SQL))
    assert len(database.queries) == 2


class SpyCache(MemoryCache):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        self.reads.extend(keys)
        return super().get_many(keys)


def test_a_rejected_sql_never_reaches_the_cache(database: FakeDatabase,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    spy = SpyCache()
    monkeypatch.setattr(executor, "caching", lambda settings: spy)
    context = tools.AgentRunContext(settings=_settings(), retriever=None)  # type: ignore[arg-type]
    run_sql = tools._make_run_sql(context)
    asyncio.run(run_sql("DELETE FROM safe.orders"))
    assert spy.reads == [] and database.queries == []
    asyncio.run(run_sql("SELECT COUNT(*) AS n FROM safe.orders"))
    assert len(spy.reads) == 1
    # The second time the trace notes the cache.
    asyncio.run(run_sql("SELECT COUNT(*) AS n FROM safe.orders"))
    assert len(database.queries) == 1
    sql_records = [record for record in context.trace.records if record.kind == "sql"]
    assert [record.detail for record in sql_records] == ["", "命中缓存"]


# --- vectors and rerank ---


class CountingEmbedder:
    model_name = "text-embedding-v3"
    dim = 3

    def __init__(self) -> None:
        self.sent: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.sent.append(list(texts))
        return [[float(len(text)), 0.1, 1 / 3] for text in texts]


def test_only_unseen_texts_go_to_the_model() -> None:
    inner = CountingEmbedder()
    embedder = CachedEmbedder(inner, MemoryCache(), 60, "safe")
    first = embedder.embed(["a", "bb"])
    texts = ["a", "ccc", "bb", "dddd", "eeeee"]
    vectors = embedder.embed(texts)
    assert inner.sent == [["a", "bb"], ["ccc", "dddd", "eeeee"]]
    # Same order as asked, and a cached vector equals the one the model gave.
    assert vectors == [[float(len(text)), 0.1, 1 / 3] for text in texts]
    assert vectors[0] == first[0]
    assert embedder.embed([]) == []


def test_another_model_does_not_share_vectors() -> None:
    memory = MemoryCache()
    CachedEmbedder(CountingEmbedder(), memory, 60, "safe").embed(["a"])
    other = CountingEmbedder()
    other.model_name = "text-embedding-v4"
    CachedEmbedder(other, memory, 60, "safe").embed(["a"])
    assert other.sent == [["a"]]


class CountingReranker:
    model_name = "gte-rerank"

    def __init__(self) -> None:
        self.calls = 0

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankResult]:
        self.calls += 1
        return [RerankResult(index=index, score=1.0 - index / 10)
                for index in range(min(top_n, len(documents)))]


def test_rerank_reuses_only_the_same_documents_in_the_same_order() -> None:
    inner = CountingReranker()
    reranker = CachedReranker(inner, MemoryCache(), 60, "safe")
    first = reranker.rerank("退款率", ["orders", "refunds"], 2)
    assert reranker.rerank("退款率", ["orders", "refunds"], 2) == first
    assert inner.calls == 1
    reranker.rerank("退款率", ["refunds", "orders"], 2)
    reranker.rerank("退款率", ["orders", "refunds"], 1)
    assert inner.calls == 3


def test_a_failed_rerank_is_not_cached() -> None:
    class Failing(CountingReranker):
        def rerank(self, query: str, documents: Sequence[str],
                   top_n: int) -> list[RerankResult]:
            self.calls += 1
            raise RuntimeError("timeout")

    inner = Failing()
    reranker = CachedReranker(inner, MemoryCache(), 60, "safe")
    for _ in range(2):
        with pytest.raises(RuntimeError):
            reranker.rerank("q", ["a"], 1)
    assert inner.calls == 2


# --- rate limit ---


def test_the_request_over_the_limit_is_refused_until_the_next_minute() -> None:
    memory = MemoryCache()
    subjects = {"session": "s1", "ip": "10.0.0.1"}
    for _ in range(20):
        assert limit_exceeded(memory, "safe", 20, subjects, now=120.0) is None
    assert limit_exceeded(memory, "safe", 20, subjects, now=150.0) == 30
    assert limit_exceeded(memory, "safe", 20, subjects, now=180.0) is None


def test_a_new_session_id_does_not_get_round_the_ip_limit() -> None:
    memory = MemoryCache()
    for index in range(2):
        assert limit_exceeded(memory, "safe", 2, {"session": f"s{index}", "ip": "ip"},
                              now=0.0) is None
    assert limit_exceeded(memory, "safe", 2, {"session": "fresh", "ip": "ip"},
                          now=0.0) is not None


def test_no_limit_or_no_counter_lets_requests_through() -> None:
    memory = MemoryCache()
    for _ in range(5):
        assert limit_exceeded(memory, "safe", 0, {"ip": "ip"}) is None
    broken = RedisCache(BrokenRedis())  # type: ignore[arg-type]
    for _ in range(5):
        assert limit_exceeded(broken, "safe", 1, {"ip": "ip"}) is None


class StubPipeline:
    def __init__(self) -> None:
        self.asked = 0

    def ask(self, question: str, session_id: str, emit: Any = None, mode: str = "query",
            history: Any = None) -> AnswerPayload:
        self.asked += 1
        return AnswerPayload(question=question)


def _client(monkeypatch: pytest.MonkeyPatch, limit: int) -> tuple[TestClient, StubPipeline]:
    monkeypatch.setattr(cache_module, "_local_limits", MemoryCache())
    app = FastAPI()
    app.include_router(router)
    app.state.settings = _settings(redis_url="", rate_limit_per_minute=limit)
    app.state.pipeline = StubPipeline()
    return TestClient(app), app.state.pipeline


def test_ask_answers_429_over_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    client, pipeline = _client(monkeypatch, 2)
    body = {"question": "上个月GMV", "session_id": "s1"}
    assert [client.post("/api/ask", json=body).status_code for _ in range(2)] == [200, 200]
    refused = client.post("/api/ask", json=body)
    assert refused.status_code == 429
    assert "每分钟最多 2 个问题" in refused.json()["detail"]
    assert int(refused.headers["Retry-After"]) >= 1
    assert pipeline.asked == 2


def test_the_stream_is_refused_before_it_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    client, pipeline = _client(monkeypatch, 1)
    body = {"question": "上个月GMV", "session_id": "s1"}
    client.post("/api/ask", json=body)
    refused = client.post("/api/ask/stream", json=body)
    assert refused.status_code == 429
    assert refused.headers["content-type"].startswith("application/json")
    assert pipeline.asked == 1
