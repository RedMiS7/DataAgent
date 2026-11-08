from __future__ import annotations

import asyncio

from agentscope.agent import Agent, ReActConfig
from agentscope.embedding import DashScopeEmbeddingModel
from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.model import DashScopeChatModel, DeepSeekChatModel
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, Toolkit
from app.agent.pipeline import extract_text
from app.agent.scope_model import (
    ScriptedChatModel,
    build_chat_model,
    build_embedding_model,
)
from app.agent.tools import AgentRunContext, _make_run_sql
from app.config import Settings

ALLOW = PermissionDecision(behavior=PermissionBehavior.ALLOW, message="test tool")


def make_settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_deepseek_chat_model_preset() -> None:
    model = build_chat_model(make_settings(llm_provider="deepseek", llm_api_key="test"))
    assert isinstance(model, DeepSeekChatModel)
    assert model.model == "deepseek-chat"


def test_chat_models_take_the_output_limit_and_timeout_from_settings() -> None:
    for provider in ("deepseek", "qwen", "openai"):
        model = build_chat_model(make_settings(
            llm_provider=provider, llm_endpoint="https://example.com/v1", llm_model="m",
            llm_api_key="test", llm_max_tokens=6000, llm_timeout_seconds=42))
        assert model is not None
        assert model.parameters.max_tokens == 6000
        assert model.client_kwargs == {"timeout": 42}


def test_output_limit_fits_a_long_analysis_sql() -> None:
    # 1024 tokens cut off a long analysis SQL mid-statement.
    assert make_settings().llm_max_tokens >= 4096


def test_explicit_endpoint_overrides_preset() -> None:
    model = build_chat_model(make_settings(
        llm_provider="deepseek",
        llm_endpoint="https://example.com/v1",
        llm_model="custom-model",
        llm_api_key="test",
    ))
    assert model is not None
    assert model.model == "custom-model"


def test_qwen_chat_model_with_explicit_config() -> None:
    model = build_chat_model(make_settings(
        llm_provider="qwen",
        llm_endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
        llm_model="qwen-plus",
        llm_api_key="test",
    ))
    assert isinstance(model, DashScopeChatModel)
    assert model.model == "qwen-plus"


def test_unknown_provider_without_endpoint_returns_none() -> None:
    assert build_chat_model(make_settings(llm_provider="maas")) is None


def test_fake_provider_uses_scripted_model() -> None:
    model = build_chat_model(make_settings(llm_provider="fake"))
    assert isinstance(model, ScriptedChatModel)


def test_qwen_embedding_model_preset() -> None:
    model = build_embedding_model(make_settings(embedding_provider="qwen",
                                                embedding_api_key="test"))
    assert isinstance(model, DashScopeEmbeddingModel)
    assert model.model == "text-embedding-v3"
    assert model.dimensions == 1024
    assert model.batch_size == 10


def test_fake_embedding_returns_none() -> None:
    assert build_embedding_model(make_settings(embedding_provider="fake")) is None


def test_agent_react_loop_calls_tool() -> None:
    calls: list[tuple[int, int]] = []

    def add(a: int, b: int) -> str:
        """Add two integers.

        Args:
            a: first integer.
            b: second integer.
        """
        calls.append((a, b))
        return str(a + b)

    model = ScriptedChatModel([
        [ToolCallBlock(type="tool_call", id="call-1", name="add",
                       input='{"a": 1, "b": 2}')],
        [TextBlock(text="结果是 3")],
    ])
    agent = Agent(
        name="tester",
        system_prompt="Call the add tool then answer.",
        model=model,
        toolkit=Toolkit([FunctionTool(add, name="add", description="add integers",
                                      permission=ALLOW, is_read_only=True)]),
        react_config=ReActConfig(max_iters=3),
    )
    reply = asyncio.run(agent.reply(
        Msg(name="user", content=[TextBlock(text="1+2")], role="user")))
    assert calls == [(1, 2)]
    assert extract_text(reply) == "结果是 3"


def test_run_sql_tool_blocks_write() -> None:
    context = AgentRunContext(settings=make_settings(), retriever=None)  # type: ignore[arg-type]
    tool = _make_run_sql(context)
    result = asyncio.run(tool("DELETE FROM safe.orders"))
    assert "SQL 校验失败" in result
    assert context.executions == []
    assert context.errors


def test_extract_text_joins_blocks() -> None:
    message = Msg(name="dataagent", role="assistant",
                  content=[TextBlock(text="a"), TextBlock(text="b")])
    assert extract_text(message) == "ab"


def test_scripted_model_replays_script() -> None:
    model = ScriptedChatModel([[TextBlock(text="first")], [TextBlock(text="second")]])
    agent = Agent(name="probe", system_prompt="reply", model=model)
    first = asyncio.run(agent.reply(Msg(name="user", content=[TextBlock(text="a")],
                                        role="user")))
    second = asyncio.run(agent.reply(Msg(name="user", content=[TextBlock(text="b")],
                                         role="user")))
    assert extract_text(first) == "first"
    assert extract_text(second) == "second"
