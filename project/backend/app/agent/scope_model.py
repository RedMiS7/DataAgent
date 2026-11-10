"""AgentScope model builders.

Chat and embedding models both come from AgentScope 2.x so the project does not
maintain its own provider clients. ``ScriptedChatModel`` is used by tests and by
the offline demo mode.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx
from agentscope.agent import Agent, ReActConfig
from agentscope.credential import DashScopeCredential, DeepSeekCredential, OpenAICredential
from agentscope.embedding import (
    DashScopeEmbeddingModel,
    EmbeddingModelBase,
    EmbeddingResponse,
    EmbeddingUsage,
    OpenAIEmbeddingModel,
)
from agentscope.message import DataBlock, Msg, TextBlock, ToolCallBlock
from agentscope.model import (
    ChatModelBase,
    ChatResponse,
    DashScopeChatModel,
    DeepSeekChatModel,
    OpenAIChatModel,
)

from ..config import Settings
from ..providers import resolve_embedding, resolve_llm
from ..service_calls import EMBEDDING_TIMEOUT_SECONDS, remaining_timeout, retry_async

logger = logging.getLogger(__name__)
QWEN_PROVIDERS = {"qwen", "dashscope"}
OPENAI_COMPAT_PROVIDERS = {"maas", "openai", "custom"}


class _RetryChat:
    async def _call_api(self, *args: Any, **kwargs: Any) -> Any:
        call = super()._call_api
        return await retry_async("model", self.client_kwargs["timeout"],
                                 lambda seconds: call(*args, **{**kwargs, "timeout": seconds}))


class _DeepSeekChat(_RetryChat, DeepSeekChatModel):
    pass


class _DashScopeChat(_RetryChat, DashScopeChatModel):
    pass


class _OpenAIChat(_RetryChat, OpenAIChatModel):
    pass


class _DashScopeEmbedding(DashScopeEmbeddingModel):
    def __init__(self, endpoint: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        base = endpoint.rstrip("/")
        if base.endswith("/compatible-mode/v1"):
            base = base.removesuffix("/compatible-mode/v1") + "/api/v1"
        task = "multimodal-embedding" if self.supports_multimodal else "text-embedding"
        path = f"/services/embeddings/{task}/{task}"
        self.endpoint = base if base.endswith(path) else base + path
        self.client = httpx.AsyncClient()

    async def _call_api(self, inputs: list[str | DataBlock], **kwargs: Any) -> EmbeddingResponse:
        # AgentScope's native adapter uses a worker thread and an SDK with its own
        # resend. Keep Bailian's native payload while making the HTTP call cancellable.
        if self.supports_multimodal:
            input_data = {"contents": [{"text": item} if isinstance(item, str)
                                       else self._format_data_block(item) for item in inputs]}
        else:
            if not all(isinstance(item, str) for item in inputs):
                raise ValueError("文本向量模型只接受字符串输入")
            input_data = {"texts": inputs}
        body = {"model": self.model, "input": input_data,
                "parameters": {"dimension": self.dimensions, **kwargs}}
        started = time.monotonic()

        async def post(seconds: float) -> httpx.Response:
            response = await self.client.post(
                self.endpoint, json=body, timeout=seconds,
                headers={"Authorization": f"Bearer {self.api_key}"})
            response.raise_for_status()
            return response

        response = await retry_async("embedding", EMBEDDING_TIMEOUT_SECONDS, post)
        data = response.json()
        usage = data.get("usage") or {}
        tokens = usage.get("total_tokens")
        if tokens is None:
            tokens = (usage.get("input_tokens") or 0) + (usage.get("image_tokens") or 0)
        return EmbeddingResponse(
            embeddings=[item["embedding"] for item in data["output"]["embeddings"]],
            usage=EmbeddingUsage(tokens=tokens, time=time.monotonic() - started),
        )


class _OpenAIEmbedding(OpenAIEmbeddingModel):
    async def _call_api(self, inputs: list, **kwargs: Any) -> Any:
        call = super()._call_api
        return await retry_async(
            "embedding", EMBEDDING_TIMEOUT_SECONDS,
            lambda seconds: call(inputs, **{**kwargs, "timeout": seconds}))


async def close_model(model: ChatModelBase | EmbeddingModelBase) -> None:
    """Release the SDK client in the loop that used it."""
    client = getattr(model, "client", None)
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if close is not None:
        try:
            await close()
        except Exception:
            logger.warning("关闭模型客户端失败 model=%s", model.model, exc_info=True)


def run_model[T](model: ChatModelBase, work: Callable[[], Awaitable[T]], timeout: float) -> T:
    """Run a chat phase and close the client before asyncio.run closes its loop."""
    async def run() -> T:
        try:
            seconds = remaining_timeout(timeout)
            return await asyncio.wait_for(work(), seconds)
        finally:
            await close_model(model)

    return asyncio.run(run())


class ScriptedChatModel(DeepSeekChatModel):
    """Deterministic model that replays a script of content blocks."""

    def __init__(self, script: Sequence[Sequence[Any]], model: str = "scripted") -> None:
        super().__init__(
            credential=DeepSeekCredential(api_key="scripted"),
            model=model,
            parameters=DeepSeekChatModel.Parameters(temperature=0.0),
            stream=False,
        )
        self._script = list(script)
        self.calls = 0

    async def _call_api(self, model: str, messages: list | None = None,
                        tools: list[dict] | None = None,
                        tool_choice: Any = None, **kwargs: Any) -> ChatResponse:
        index = min(self.calls, len(self._script) - 1)
        self.calls += 1
        return ChatResponse(content=list(self._script[index]), is_last=True)


def offline_scripted_model() -> ScriptedChatModel:
    return ScriptedChatModel([
        [ToolCallBlock(type="tool_call", id="offline-call-1", name="run_sql",
                       input='{"sql": "SELECT 1 AS result"}')],
        [TextBlock(text="离线模式：已完成一次固定查询，仅用于验证链路。")],
    ])


def _credential(cls: type, api_key: str, endpoint: str) -> Any:
    kwargs: dict[str, Any] = {"api_key": api_key}
    if endpoint:
        kwargs["base_url"] = endpoint
    return cls(**kwargs)


def build_chat_model(settings: Settings) -> ChatModelBase | None:
    provider = settings.llm_provider.lower()
    if provider == "fake":
        return offline_scripted_model()

    endpoint, model_name = resolve_llm(provider, settings.llm_endpoint, settings.llm_model)
    if not endpoint or not model_name:
        return None
    if not settings.llm_api_key.strip():
        return None

    explicit_endpoint = settings.llm_endpoint.strip()
    temperature = settings.llm_temperature
    max_tokens = settings.llm_max_tokens
    # One model call; the whole question has its own limit in the pipeline.
    client_kwargs: dict[str, Any] = {"timeout": settings.llm_timeout_seconds, "max_retries": 0}

    if provider == "deepseek":
        return _DeepSeekChat(
            credential=_credential(DeepSeekCredential, settings.llm_api_key, explicit_endpoint),
            model=model_name,
            parameters=DeepSeekChatModel.Parameters(temperature=temperature, max_tokens=max_tokens),
            stream=False,
            max_retries=0,
            client_kwargs=client_kwargs,
        )
    if provider in QWEN_PROVIDERS:
        return _DashScopeChat(
            credential=_credential(DashScopeCredential, settings.llm_api_key, explicit_endpoint),
            model=model_name,
            parameters=DashScopeChatModel.Parameters(temperature=temperature, max_tokens=max_tokens),
            stream=False,
            max_retries=0,
            client_kwargs=client_kwargs,
        )
    return _OpenAIChat(
        credential=_credential(OpenAICredential, settings.llm_api_key, endpoint),
        model=model_name,
        parameters=OpenAIChatModel.Parameters(temperature=temperature, max_tokens=max_tokens),
        stream=False,
        max_retries=0,
        client_kwargs=client_kwargs,
    )


def probe_chat_model(model: ChatModelBase) -> str:
    """Send one short message and return the reply; failures raise."""
    agent = Agent(name="probe", system_prompt="你只需要回复 OK。", model=model,
                  react_config=ReActConfig(interruption_raise_cancelled_error=True))
    message = Msg(name="user", content=[TextBlock(text="只回复 OK")], role="user")
    timeout = getattr(model, "client_kwargs", {}).get("timeout", 30.0)
    reply = run_model(model, lambda: agent.reply(message), timeout)
    return "".join(block.text for block in reply.content
                   if isinstance(block, TextBlock)).strip()


def build_embedding_model(settings: Settings) -> EmbeddingModelBase | None:
    provider = settings.embedding_provider.lower()
    if provider == "fake":
        return None

    endpoint, model_name, dim, batch_size = resolve_embedding(
        provider,
        settings.embedding_endpoint,
        settings.embedding_model,
        settings.embedding_dim,
        settings.embedding_batch_size,
    )
    if not endpoint or not model_name:
        return None
    if not settings.embedding_api_key.strip():
        return None

    if provider in QWEN_PROVIDERS:
        model = _DashScopeEmbedding(
            endpoint=endpoint,
            credential=_credential(DashScopeCredential, settings.embedding_api_key, endpoint),
            model=model_name,
            dimensions=dim,
            max_retries=0,
        )
    else:
        model = _OpenAIEmbedding(
            credential=_credential(OpenAICredential, settings.embedding_api_key, endpoint),
            model=model_name,
            dimensions=dim,
            max_retries=0,
        )
        model.client.max_retries = 0
    model.batch_size = batch_size
    return model
