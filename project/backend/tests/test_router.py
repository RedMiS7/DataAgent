from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from agentscope.message import TextBlock
from app.agent import router
from app.agent.router import build_router_input, parse_route, route_question
from app.agent.scope_model import ScriptedChatModel
from app.api.schemas import HistoryMessage


def scripted(text: str) -> ScriptedChatModel:
    return ScriptedChatModel([[TextBlock(text=text)]])


@pytest.mark.parametrize("text, expected", [
    ('{"route": "analysis", "reason": "问原因"}', ("analysis", "问原因")),
    ('```json\n{"route": "query", "reason": "要数字"}\n```', ("query", "要数字")),
    ('结果：{"route": "query"}', ("query", "")),
])
def test_parse_route_reads_the_json(text: str, expected: tuple[str, str]) -> None:
    assert parse_route(text) == expected


@pytest.mark.parametrize("text", [
    "analysis",
    '{"route": "plan", "reason": "x"}',
    '{"route": "query"',
    '["query"]',
])
def test_parse_route_rejects_bad_replies(text: str) -> None:
    with pytest.raises(router.RouteParseError):
        parse_route(text)


def test_the_router_input_labels_each_reply_with_its_mode() -> None:
    history = [
        HistoryMessage(role="user", content="最近生意怎么样？"),
        HistoryMessage(role="assistant", content="整体平稳。[查询1]", mode="analysis"),
        HistoryMessage(role="user", content="上个月 GMV"),
        HistoryMessage(role="assistant", content="966.6 万", mode="query"),
    ]
    assert build_router_input("那订单数呢？", history) == (
        "对话记录（从早到晚）：\n"
        "用户：最近生意怎么样？\n"
        "助手（分析模式）：整体平稳。\n"
        "用户：上个月 GMV\n"
        "助手（查询模式）：966.6 万\n\n"
        "最后一个问题：那订单数呢？"
    )


def test_the_router_input_says_when_a_reply_found_nothing() -> None:
    history = [
        HistoryMessage(role="user", content="上个月各省份的退款金额？"),
        HistoryMessage(role="assistant", content="超过 120 秒还没有查到数据。", mode="query",
                       status="error"),
    ]
    assert "助手（查询模式）：（这一轮没有查到结果：超过 120 秒还没有查到数据。）" in \
        build_router_input("上个月订单数？", history)


def test_the_router_input_without_history() -> None:
    assert build_router_input("上个月 GMV", []).startswith("对话记录（从早到晚）：\n（无）")


def test_route_question_returns_the_model_choice() -> None:
    decision = route_question(scripted('{"route": "analysis", "reason": "问原因"}'), "为什么", [])
    assert (decision.route, decision.reason, decision.source) == ("analysis", "问原因", "llm")


def test_a_long_reason_is_cut() -> None:
    decision = route_question(scripted('{"route": "query", "reason": "' + "长" * 100 + '"}'),
                              "问题", [])
    assert len(decision.reason) == router.REASON_LIMIT


def test_an_unreadable_reply_falls_back_and_logs_it(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app"):
        decision = route_question(scripted("应该是分析吧"), "问题", [], "abc123")
    assert (decision.route, decision.source) == ("query", "fallback")
    line = caplog.records[-1].getMessage()
    assert line.startswith("[abc123] 路由结果无法解析")
    assert "应该是分析吧" in line


def test_a_model_error_falls_back_and_logs_the_traceback(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    async def boom(model: Any, content: str) -> str:
        raise RuntimeError("模型不可用")

    monkeypatch.setattr(router, "_ask_model", boom)
    with caplog.at_level(logging.WARNING, logger="app"):
        decision = route_question(scripted(""), "问题", [])
    assert (decision.route, decision.source) == ("query", "fallback")
    assert caplog.records[-1].exc_info is not None


class SlowModel(ScriptedChatModel):
    async def _call_api(self, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(1)
        return await super()._call_api(*args, **kwargs)


def test_a_slow_model_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    # The model itself is slow, so the agent is cancelled mid-reply as in production.
    model = SlowModel([[TextBlock(text='{"route": "analysis"}')]])
    with caplog.at_level(logging.WARNING, logger="app"):
        decision = route_question(model, "问题", [], timeout=0.05)
    assert (decision.route, decision.source) == ("query", "fallback")
    assert "超时" in decision.reason
    assert "路由超时" in caplog.records[-1].getMessage()
