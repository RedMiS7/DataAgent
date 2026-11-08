"""AgentScope model builders.

Chat and embedding models both come from AgentScope 2.x so the project does not
maintain its own provider clients. ``ScriptedChatModel`` is used by tests and by
the offline demo mode.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from agentscope.agent import Agent
from agentscope.credential import DashScopeCredential, DeepSeekCredential, OpenAICredential
from agentscope.embedding import DashScopeEmbeddingModel, EmbeddingModelBase, OpenAIEmbeddingModel
from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.model import (
    ChatModelBase,
    ChatResponse,
    DashScopeChatModel,
    DeepSeekChatModel,
    OpenAIChatModel,
)

from ..config import Settings
from ..providers import resolve_embedding, resolve_llm

QWEN_PROVIDERS = {"qwen", "dashscope"}
OPENAI_COMPAT_PROVIDERS = {"maas", "openai", "custom"}


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
    client_kwargs: dict[str, Any] = {"timeout": settings.llm_timeout_seconds}

    if provider == "deepseek":
        return DeepSeekChatModel(
            credential=_credential(DeepSeekCredential, settings.llm_api_key, explicit_endpoint),
            model=model_name,
            parameters=DeepSeekChatModel.Parameters(temperature=temperature, max_tokens=max_tokens),
            stream=False,
            client_kwargs=client_kwargs,
        )
    if provider in QWEN_PROVIDERS:
        return DashScopeChatModel(
            credential=_credential(DashScopeCredential, settings.llm_api_key, explicit_endpoint),
            model=model_name,
            parameters=DashScopeChatModel.Parameters(temperature=temperature, max_tokens=max_tokens),
            stream=False,
            client_kwargs=client_kwargs,
        )
    return OpenAIChatModel(
        credential=_credential(OpenAICredential, settings.llm_api_key, endpoint),
        model=model_name,
        parameters=OpenAIChatModel.Parameters(temperature=temperature, max_tokens=max_tokens),
        stream=False,
        client_kwargs=client_kwargs,
    )


def probe_chat_model(model: ChatModelBase) -> str:
    """Send one short message and return the reply; failures raise."""
    agent = Agent(name="probe", system_prompt="你只需要回复 OK。", model=model)
    reply = asyncio.run(agent.reply(Msg(name="user", content=[TextBlock(text="只回复 OK")],
                                        role="user")))
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
        model = DashScopeEmbeddingModel(
            credential=_credential(DashScopeCredential, settings.embedding_api_key, endpoint),
            model=model_name,
            dimensions=dim,
        )
    else:
        model = OpenAIEmbeddingModel(
            credential=_credential(OpenAICredential, settings.embedding_api_key, endpoint),
            model=model_name,
            dimensions=dim,
        )
    model.batch_size = batch_size
    return model