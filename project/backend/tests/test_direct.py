"""Questions answered without SQL, and follow-ups that keep the last time range."""

from __future__ import annotations

import itertools
import json
from contextlib import contextmanager
from datetime import date
from typing import Any

import pytest
from agentscope.message import TextBlock, ToolCallBlock
from app.agent import pipeline, router, session, tools
from app.agent.pipeline import NEED_DATA, AskPipeline, is_followup, resolve_time_range
from app.agent.router import build_router_input, parse_route
from app.agent.scope_model import ScriptedChatModel
from app.api.schemas import HistoryMessage, HistoryQuery
from app.config import Settings
from app.nl2sql.executor import ExecutionResult
from app.semantic.models import Metric, MetricHit, RetrievalBundle

AS_OF = date(2026, 10, 31)
CALL_IDS = itertools.count(1)
SEPTEMBER_SQL = ("SELECT COUNT(DISTINCT o.id) AS 订单数 FROM safe.orders o "
                 "WHERE o.paid_at >= '2026-09-01' AND o.paid_at < '2026-10-01'")
GMV = Metric(name="GMV", definition="支付成功且未取消订单的商品金额合计，不含运费",
             formula="SUM(oi.quantity * oi.unit_price - oi.discount_amount)",
             related_tables="safe.orders,safe.order_items")
GMV_HISTORY = [HistoryMessage(role="user", content="上个月GMV是多少？"),
               HistoryMessage(role="assistant", content="上个月 GMV 为 412.3 万元。", mode="query",
                              status="ok")]


def text(value: str) -> list[TextBlock]:
    return [TextBlock(text=value)]


def call(name: str, **arguments: str) -> ToolCallBlock:
    return ToolCallBlock(type="tool_call", id=f"direct-{next(CALL_IDS)}", name=name,
                         input=json.dumps(arguments, ensure_ascii=False))


def route(choice: str) -> list[TextBlock]:
    return text(json.dumps({"route": choice, "reason": "测试"}, ensure_ascii=False))


class OneMetricRetriever:
    def retrieve_with_connection(self, question: str, conn: object) -> RetrievalBundle:
        return RetrievalBundle(metrics=[MetricHit(metric=GMV, score=0.9)])


@pytest.fixture
def ask(monkeypatch: pytest.MonkeyPatch):
    """The real pipeline; models are handed out in the order the pipeline builds them:
    the query agent's, the router's, then the direct answer's."""
    @contextmanager
    def no_connection(settings: Settings):
        yield None

    executed: list[str] = []

    def fake_execute(settings: Settings, safe: Any) -> ExecutionResult:
        executed.append(safe.sql)
        return ExecutionResult(columns=["订单数"], rows=[[15345]], sql=safe.sql)

    monkeypatch.setattr(pipeline, "app_connection", no_connection)
    monkeypatch.setattr(tools, "execute_sql", fake_execute)
    monkeypatch.setattr(session, "save_query_log", lambda *args: 1)
    monkeypatch.setattr(session, "save_call_traces", lambda *args: None)

    def run(question: str, *, agent: list[Any], router_reply: list[Any] | None = None,
            direct: list[Any] | None = None, mode: str = "auto",
            history: list[HistoryMessage] | None = None):
        models = iter([ScriptedChatModel(agent), ScriptedChatModel([router_reply or []]),
                       ScriptedChatModel(direct or [text("")])])
        monkeypatch.setattr(pipeline, "build_chat_model", lambda settings: next(models))
        settings = Settings(_env_file=None, as_of_date=AS_OF)
        answer = AskPipeline(settings, OneMetricRetriever()).ask(  # type: ignore[arg-type]
            question, "test", None, mode, history or [])  # type: ignore[arg-type]
        return answer, executed

    return run


# Routing

def test_the_router_accepts_direct() -> None:
    assert parse_route('{"route": "direct", "reason": "解释口径"}') == ("direct", "解释口径")
    assert "direct" in router.ROUTER_PROMPT


def test_the_router_labels_a_direct_reply() -> None:
    history = [HistoryMessage(role="user", content="GMV包不包括运费？"),
               HistoryMessage(role="assistant", content="不包括。", mode="query", status="text")]
    assert "助手（直接回答）：不包括。" in build_router_input("那退款呢？", history)


# Direct answers

def test_a_direct_question_is_answered_without_sql(ask) -> None:
    answer, executed = ask("GMV包不包括运费？", agent=[text("不应该用到")],
                           router_reply=route("direct"),
                           direct=[text("不包括。GMV 只算商品金额，不含运费。")])
    assert answer.status == "text"
    assert answer.summary == "不包括。GMV 只算商品金额，不含运费。"
    assert answer.summary_source == "model"
    assert answer.sql == "" and answer.table is None and executed == []
    assert answer.route is not None and answer.route.chosen == "direct"
    assert answer.mode == "query"
    # The definition the answer rests on is shown.
    assert [metric.name for metric in answer.metrics] == ["GMV"]
    assert answer.warnings == []


def test_a_rewrite_may_repeat_the_figures_already_given(ask) -> None:
    answer, _ = ask("把这个结论写成一段话发给老板", agent=[text("不应该用到")],
                    router_reply=route("direct"),
                    direct=[text("老板您好，上个月 GMV 为 412.3万元。")], history=GMV_HISTORY)
    assert answer.status == "text"
    assert answer.warnings == []


def test_a_figure_from_nowhere_is_flagged(ask) -> None:
    answer, _ = ask("把这个结论写成一段话发给老板", agent=[text("不应该用到")],
                    router_reply=route("direct"),
                    direct=[text("上个月 GMV 为 412.3 万元，比上上个月多 35.6 万元。")],
                    history=GMV_HISTORY)
    assert answer.status == "text"
    assert answer.warnings == ["这些数字没有查询依据：35.6万"]


def test_a_number_only_in_earlier_sql_is_no_source(ask) -> None:
    history = [GMV_HISTORY[0], GMV_HISTORY[1].model_copy(update={"queries": [HistoryQuery(
        purpose="上个月GMV", sql="SELECT SUM(x) FROM safe.orders LIMIT 500")]})]
    answer, _ = ask("把这个结论写成一段话发给老板", agent=[text("不应该用到")],
                    router_reply=route("direct"),
                    direct=[text("上个月 GMV 为 412.3 万元，共 500 个商品。")], history=history)
    assert answer.warnings == ["这些数字没有查询依据：500"]


def test_a_direct_answer_that_needs_data_falls_back_to_a_query(ask) -> None:
    answer, executed = ask("上个月订单数是多少？",
                           agent=[[call("run_sql", sql=SEPTEMBER_SQL)],
                                  text("上个月订单数为 15345 单。")],
                           router_reply=route("direct"), direct=[text(NEED_DATA)])
    assert answer.status == "ok"
    assert executed and "2026-09-01" in executed[0]
    assert answer.route is not None and answer.route.chosen == "query"
    assert "回答时发现需要查数" in answer.route.reason


def test_query_mode_may_answer_a_definition_without_sql(ask) -> None:
    answer, executed = ask("GMV包不包括运费？", agent=[text("不包括，GMV 不含运费。")],
                           mode="query")
    assert answer.status == "text"
    assert answer.route is None
    assert executed == []


# Follow-ups and time

@pytest.mark.parametrize("question, expected", [
    ("按渠道拆一下", True),
    ("把这个结论写成一段话发给老板", True),
    ("只看直播渠道", True),
    ("加上GMV", True),
    ("上面的数据按省份细分", True),
    ("这个月GMV是多少？", False),
    ("哪些商品退款最多？", False),
    ("一共有几个渠道", False),
])
def test_more_followups_are_recognised(question: str, expected: bool) -> None:
    assert is_followup(question) is expected


def test_a_breakdown_followup_keeps_the_last_time() -> None:
    history = [("user", "上个月GMV是多少？"), ("assistant", "上个月 GMV 为 412.3 万元。")]
    assert resolve_time_range("按渠道拆一下", history, AS_OF).label == "上个月"
