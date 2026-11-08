from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from agentscope.agent import Agent, ReActConfig
from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.model import ChatResponse, ChatUsage
from agentscope.tool import FunctionTool, Toolkit
from app.agent import router, session
from app.agent.scope_model import ScriptedChatModel
from app.agent.tools import READ_ONLY_PERMISSION
from app.agent.trace import CallTrace
from app.semantic.cli import format_trace

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "data" / "initdb" / "02_app_schema.sql"


class UsageModel(ScriptedChatModel):
    """Replays the script and reports 100 input (60 from the cache) and 10 output tokens
    per call."""

    async def _call_api(self, *args: Any, **kwargs: Any) -> ChatResponse:
        response = await super()._call_api(*args, **kwargs)
        response.usage = ChatUsage(input_tokens=100, output_tokens=10, time=0.0,
                                   cache_input_tokens=60)
        return response


class BrokenModel(ScriptedChatModel):
    async def _call_api(self, *args: Any, **kwargs: Any) -> ChatResponse:
        raise RuntimeError("模型不可用")


class SlowModel(ScriptedChatModel):
    async def _call_api(self, *args: Any, **kwargs: Any) -> ChatResponse:
        await asyncio.sleep(1)
        return await super()._call_api(*args, **kwargs)


def add(a: int, b: int) -> str:
    """Add two integers.

    Args:
        a: first integer.
        b: second integer.
    """
    return str(a + b)


def run_agent(model: Any, trace: CallTrace) -> Msg:
    agent = Agent(
        name="tester", system_prompt="Call add then answer.", model=model,
        toolkit=Toolkit([FunctionTool(add, name="add", description="add integers",
                                      permission=READ_ONLY_PERMISSION, is_read_only=True)]),
        react_config=ReActConfig(max_iters=3), middlewares=trace.middlewares(),
    )
    return asyncio.run(agent.reply(Msg(name="user", content=[TextBlock(text="1+2")], role="user")))


def test_model_and_tool_calls_are_recorded_with_tokens() -> None:
    trace = CallTrace()
    run_agent(UsageModel([
        [ToolCallBlock(type="tool_call", id="trace-1", name="add", input='{"a": 1, "b": 2}')],
        [TextBlock(text="结果是 3")],
    ]), trace)
    records = trace.ordered()
    assert [(item.agent, item.kind, item.name) for item in records] == [
        ("tester", "model", "scripted"), ("tester", "tool", "add"), ("tester", "model", "scripted")]
    assert records[0].detail == "add"
    assert (records[0].input_tokens, records[0].output_tokens) == (100, 10)
    assert records[0].cached_tokens == 60
    assert all(item.ok for item in records)
    assert records[0].started_ms <= records[1].started_ms <= records[2].started_ms
    assert trace.usage() == {"model_calls": 2, "tool_calls": 1, "input_tokens": 200,
                             "output_tokens": 20, "cached_input_tokens": 120,
                             "model_ms": trace.usage()["model_ms"]}


def test_a_failed_model_call_is_recorded_and_raised() -> None:
    trace = CallTrace()
    with pytest.raises(RuntimeError, match="模型不可用"):
        run_agent(BrokenModel([[]]), trace)
    assert len(trace.records) == 1
    record = trace.records[0]
    assert (record.kind, record.ok) == ("model", False)
    assert record.detail == "RuntimeError: 模型不可用"


def test_a_timed_out_router_call_is_recorded() -> None:
    trace = CallTrace()
    decision = router.route_question(SlowModel([[TextBlock(text='{"route": "query"}')]]), "问题",
                                     [], timeout=0.05, trace=trace)
    assert decision.source == "fallback"
    assert [(item.agent, item.ok) for item in trace.records] == [("router", False)]
    assert trace.records[0].detail == "interrupted"


def test_a_router_call_is_recorded() -> None:
    trace = CallTrace()
    router.route_question(UsageModel([[TextBlock(text='{"route": "query", "reason": "数字"}')]]),
                          "上个月 GMV", [], trace=trace)
    assert [(item.agent, item.kind, item.input_tokens) for item in trace.records] == [
        ("router", "model", 100)]


def _statement(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_the_startup_ddl_matches_the_schema_file() -> None:
    assert _statement(session.CALL_TRACES_DDL) in _statement(SCHEMA_FILE.read_text(encoding="utf-8"))


def test_trace_rows_print_one_line_per_call() -> None:
    rows = [
        {"seq": 1, "agent": "planner", "kind": "model", "name": "deepseek-chat", "started_ms": 2100,
         "duration_ms": 6500, "input_tokens": 2400, "output_tokens": 180, "ok": 1, "detail": ""},
        {"seq": 2, "agent": "step_1", "kind": "tool", "name": "run_sql", "started_ms": 9000,
         "duration_ms": 120, "input_tokens": 0, "output_tokens": 0, "ok": 0, "detail": "error"},
    ]
    assert format_trace(rows) == [
        "  1  +   2.1s    6.5s  planner      model deepseek-chat tokens=2400/180",
        "  2  +   9.0s    0.1s  step_1       tool  run_sql 失败 error",
    ]
