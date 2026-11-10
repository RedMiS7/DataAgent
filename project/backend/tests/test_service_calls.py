from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from agentscope.embedding import EmbeddingModelBase
from agentscope.model import ChatModelBase, DeepSeekChatModel
from app import service_calls
from app.agent.scope_model import build_chat_model, run_model
from app.agent.trace import CallTrace
from app.config import Settings
from app.embedding import EmbeddingError, build_embedder
from app.embedding.scope_embedder import ScopeEmbedder
from app.service_calls import retry_async, service_budget


@pytest.mark.parametrize(("error", "kind"), [
    (TimeoutError("slow"), "timeout"),
    (RuntimeError('{"status_code": 429}'), "429"),
    (RuntimeError("HTTP 503"), "5xx"),
])
def test_transient_failures_get_two_exponentially_delayed_retries(
        monkeypatch: pytest.MonkeyPatch, error: Exception, kind: str) -> None:
    sleeps: list[float] = []
    timeouts: list[float] = []
    trace = CallTrace()

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def call(timeout: float) -> None:
        timeouts.append(timeout)
        raise error

    monkeypatch.setattr(service_calls.asyncio, "sleep", sleep)
    with service_budget(3, trace.retry), pytest.raises(type(error)):
        asyncio.run(retry_async("embedding", 10, call))

    assert sleeps == [0.25, 0.5] and len(timeouts) == 3
    assert all(0 < timeout <= 3 for timeout in timeouts)
    assert [(item.kind, item.name, item.error_type) for item in trace.records] == [
        ("retry", "embedding", kind), ("retry", "embedding", kind)]


@pytest.mark.parametrize(("error", "seconds"), [
    (RuntimeError("HTTP 401"), 1.0), (RuntimeError("HTTP 429"), 0.1),
    (asyncio.CancelledError(), 1.0),
])
def test_permanent_errors_cancellation_and_short_budgets_do_not_retry(
        error: BaseException, seconds: float) -> None:
    calls: list[float] = []
    trace = CallTrace()

    async def call(timeout: float) -> None:
        calls.append(timeout)
        raise error

    with service_budget(seconds, trace.retry), pytest.raises(type(error)):
        asyncio.run(retry_async("model", 2, call))
    assert len(calls) == 1 and 0 < calls[0] <= seconds
    assert not trace.records


def test_a_slow_call_is_cancelled_at_the_question_deadline() -> None:
    started: list[float] = []
    cancelled = False

    async def call(timeout: float) -> None:
        nonlocal cancelled
        started.append(timeout)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True

    with service_budget(0.05), pytest.raises(TimeoutError):
        asyncio.run(retry_async("rerank", 10, call))
    assert cancelled and len(started) == 1 and started[0] <= 0.05


class _LoopClient:
    used: asyncio.AbstractEventLoop | None = None
    closed: asyncio.AbstractEventLoop | None = None

    async def aclose(self) -> None:
        self.closed = asyncio.get_running_loop()
        assert not self.closed.is_closed()


@pytest.mark.parametrize("outcome", ["ok", "failed", "timeout", "expired"])
def test_chat_cleanup_runs_in_the_active_loop(outcome: str) -> None:
    client = _LoopClient()
    model = cast(ChatModelBase, SimpleNamespace(client=client, model="fixture"))

    async def work() -> str:
        client.used = asyncio.get_running_loop()
        if outcome == "failed":
            raise ValueError("failed")
        if outcome == "timeout":
            await asyncio.Event().wait()
        return "ok"

    if outcome == "ok":
        assert run_model(model, work, 1) == "ok"
    elif outcome == "failed":
        with pytest.raises(ValueError, match="failed"):
            run_model(model, work, 1)
    elif outcome == "timeout":
        with pytest.raises(TimeoutError):
            run_model(model, work, 0.03)
    else:
        with service_budget(0), pytest.raises(TimeoutError):
            run_model(model, work, 1)
        assert client.used is None
    assert client.closed is not None and client.closed.is_closed()
    if client.used is not None:
        assert client.closed is client.used


def test_real_chat_adapter_retries_with_the_remaining_timeout(
        monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []

    async def provider(self: DeepSeekChatModel, *args: Any, **kwargs: Any) -> str:
        timeouts.append(kwargs["timeout"])
        if len(timeouts) == 1:
            raise RuntimeError("Error code: 429")
        return "ok"

    monkeypatch.setattr(DeepSeekChatModel, "_call_api", provider)
    monkeypatch.setattr(service_calls, "BACKOFF_SECONDS", (0.0, 0.0))
    model = build_chat_model(Settings(_env_file=None, llm_api_key="fixture"))
    assert model is not None
    trace = CallTrace()
    with service_budget(2, trace.retry):
        result = run_model(model, lambda: model._call_api(model.model), 5)
    assert result == "ok" and len(timeouts) == 2
    assert all(0 < timeout <= 2 for timeout in timeouts)
    assert model.client.is_closed()
    assert [item.error_type for item in trace.records] == ["429"]


@pytest.mark.parametrize(("model_name", "field", "input_data"), [
    ("text-embedding-v3", "text-embedding", {"texts": ["测试"]}),
    ("multimodal-embedding-v1", "multimodal-embedding", {"contents": [{"text": "测试"}]}),
])
def test_bailian_embedding_retries_on_the_native_endpoint(
        monkeypatch: pytest.MonkeyPatch, model_name: str, field: str, input_data: dict) -> None:
    clients: list[httpx.AsyncClient] = []
    requests: list[dict] = []
    trace = CallTrace()

    async def post(self: httpx.AsyncClient, url: str, *, json: dict,
                   timeout: float, headers: dict) -> httpx.Response:
        assert 0 < timeout <= 1
        assert url == f"https://dashscope.aliyuncs.com/api/v1/services/embeddings/{field}/{field}"
        clients.append(self)
        requests.append(json)
        request = httpx.Request("POST", url)
        if len(requests) == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, request=request, json={
            "output": {"embeddings": [{"embedding": [1, 2]}]}, "usage": {"total_tokens": 2},
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr(service_calls, "BACKOFF_SECONDS", (0.0, 0.0))
    embedder = build_embedder(Settings(_env_file=None, embedding_api_key="fixture",
                                       embedding_model=model_name, embedding_dim=2))
    assert embedder is not None
    with service_budget(1, trace.retry):
        assert embedder.embed(["测试"]) == [[1.0, 2.0]]
    assert len(requests) == 2
    assert requests[0] == {"model": model_name, "input": input_data, "parameters": {"dimension": 2}}
    assert clients[0] is clients[1] and clients[0].is_closed
    assert [(item.name, item.error_type) for item in trace.records] == [("embedding", "5xx")]


def test_embedding_concurrency_uses_separate_clients_and_closes_them_on_failure() -> None:
    clients: list[_LoopClient] = []

    class Model:
        def __init__(self) -> None:
            self.client = _LoopClient()
            clients.append(self.client)

        async def __call__(self, texts: list[str]) -> Any:
            self.client.used = asyncio.get_running_loop()
            await asyncio.sleep(0.005)
            if texts == ["失败"]:
                raise ValueError("invalid")
            return SimpleNamespace(embeddings=[[1]])

    def model() -> EmbeddingModelBase:
        return cast(EmbeddingModelBase, Model())

    embedder = ScopeEmbedder(model, "fixture", 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ok = pool.submit(embedder.embed, ["正常"])
        failed = pool.submit(embedder.embed, ["失败"])
        assert ok.result() == [[1.0]]
        with pytest.raises(EmbeddingError, match="invalid"):
            failed.result()
    assert len(clients) == 2 and clients[0].used is not clients[1].used
    assert all(client.used is client.closed and client.closed.is_closed() for client in clients)
