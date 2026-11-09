"""Failed and partial answers: what the user gets when something goes wrong."""

from __future__ import annotations

import asyncio
import itertools
import json
from contextlib import contextmanager
from datetime import date
from typing import Any

import httpx
import openai
import pytest
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse
from app.agent import pipeline, session, tools
from app.agent.failure import explain_sql_error, is_model_error, sql_failure
from app.agent.pipeline import AskPipeline
from app.agent.scope_model import ScriptedChatModel
from app.config import Settings
from app.nl2sql.executor import ExecutionResult, SQLExecutionError
from app.semantic.models import Metric, MetricHit, RetrievalBundle

AS_OF = date(2026, 10, 31)
GOOD_SQL = ("SELECT name, qty FROM safe.products "
            "WHERE launch_date >= '2026-09-01' AND launch_date < '2026-10-01'")
BAD_SQL = "SELECT amount FROM safe.orders WHERE paid_at >= '2026-09-01'"
CALL_IDS = itertools.count(1)
HANG = "hang"
BOOM = "boom"

GMV = Metric(name="GMV", definition="支付成功且未取消订单的商品金额合计", formula="SUM(...)",
             related_tables="safe.orders,safe.order_items")


def call(name: str, **arguments: str) -> ToolCallBlock:
    return ToolCallBlock(type="tool_call", id=f"fail-{next(CALL_IDS)}", name=name,
                         input=json.dumps(arguments, ensure_ascii=False))


def text(value: str) -> list[TextBlock]:
    return [TextBlock(text=value)]


class StagedModel(ScriptedChatModel):
    """A scripted model whose script can also hang or fail like a real service."""

    async def _call_api(self, model: str, messages: list | None = None,
                        tools: list[dict] | None = None, tool_choice: Any = None,
                        **kwargs: Any) -> ChatResponse:
        step = self._script[min(self.calls, len(self._script) - 1)]
        if step == HANG:
            self.calls += 1
            await asyncio.sleep(30)
        if step == BOOM:
            self.calls += 1
            request = httpx.Request("POST", "https://llm.example/v1/chat/completions")
            raise openai.AuthenticationError("invalid api key",
                                             response=httpx.Response(401, request=request),
                                             body=None)
        return await super()._call_api(model, messages, tools, tool_choice, **kwargs)


class OneMetricRetriever:
    def retrieve_with_connection(self, question: str, conn: object) -> RetrievalBundle:
        return RetrievalBundle(metrics=[MetricHit(metric=GMV, score=0.9)])


@pytest.fixture
def ask(monkeypatch: pytest.MonkeyPatch):
    """The real pipeline and Agent loop, with a staged model and no database."""
    @contextmanager
    def no_connection(settings: Settings):
        yield None

    def fake_execute(settings: Settings, safe: Any) -> ExecutionResult:
        if "amount" in safe.sql:
            raise SQLExecutionError(
                "SQL 执行失败：(pymysql.err.OperationalError) (1054, \"Unknown column 'amount' "
                "in 'field list'\")\n[SQL: SELECT amount FROM safe.orders]", safe.sql)
        return ExecutionResult(columns=["商品", "销量"], rows=[["童装热销款208号", 176]],
                               sql=safe.sql)

    saved: dict[str, Any] = {}

    def save_log(conn: object, *args: Any, **kwargs: Any) -> int:
        saved["log"] = kwargs
        return 1

    monkeypatch.setattr(pipeline, "app_connection", no_connection)
    monkeypatch.setattr(tools, "execute_sql", fake_execute)
    monkeypatch.setattr(session, "save_query_log", save_log)
    monkeypatch.setattr(session, "save_call_traces", lambda *args: None)
    monkeypatch.setattr(session, "save_query_executions",
                        lambda conn, log_id, records: saved.__setitem__("queries", records))

    def run(script: list[Any] | None, mode: str = "query", **settings: Any):
        events: list[tuple[str, dict[str, Any]]] = []
        model = None if script is None else StagedModel(script)
        monkeypatch.setattr(pipeline, "build_chat_model", lambda value: model)
        config = Settings(_env_file=None, as_of_date=AS_OF, **settings)
        answer = AskPipeline(config, OneMetricRetriever()).ask(  # type: ignore[arg-type]
            "上个月卖得最好的商品是哪个？", "test",
            lambda event, data: events.append((event, data)), mode)  # type: ignore[arg-type]
        return answer, events

    # What would be stored for the last question: the log fields and every query.
    run.saved = saved  # type: ignore[attr-defined]
    return run


def test_failures_are_sent_as_answers(ask) -> None:
    # Neither a query nor any text: a text reply alone is now a direct answer.
    answer, events = ask([text("")])
    assert answer.status == "error"
    assert [event for event, _ in events if event in ("answer", "error")] == ["answer"]
    assert events[-1][1]["failure"]["kind"] == "no_answer"


def test_a_timeout_after_a_query_shows_what_was_found(ask) -> None:
    answer, _ = ask([[call("run_sql", sql=GOOD_SQL)], HANG], ask_timeout_seconds=0.5)
    assert answer.status == "partial"
    assert answer.failure is not None and answer.failure.kind == "timeout"
    assert "已经查到的数据" in answer.failure.message
    assert answer.table is not None and answer.table.rows == [["童装热销款208号", 176]]
    assert answer.summary_source == "template" and answer.summary
    assert "2026-09-01" in answer.sql


def test_a_degraded_answer_archives_the_failed_and_the_finished_queries(ask) -> None:
    answer, _ = ask([[call("run_sql", sql=BAD_SQL)], [call("run_sql", sql=GOOD_SQL)], HANG],
                    ask_timeout_seconds=0.5)
    assert answer.status == "partial"
    log = ask.saved["log"]
    assert log["summary"] == answer.summary != ""
    assert log["summary_source"] == "template"
    failed, ran = ask.saved["queries"]
    assert (failed.status, failed.query_no) == ("failed", None)
    assert "Unknown column 'amount'" in failed.error and "amount" in failed.sql
    assert (ran.status, ran.query_no, ran.rows) == ("ok", 1, [["童装热销款208号", 176]])


def test_an_answer_without_a_conclusion_stores_no_summary_source(ask) -> None:
    answer, _ = ask([HANG], ask_timeout_seconds=0.5)
    assert answer.status == "error"
    assert ask.saved["log"]["summary"] == ""
    assert ask.saved["log"]["summary_source"] == ""
    assert ask.saved["queries"] == []


def test_a_timeout_before_any_query_explains_itself(ask) -> None:
    answer, _ = ask([HANG], ask_timeout_seconds=0.5)
    assert answer.status == "error"
    assert answer.failure is not None and answer.failure.kind == "timeout"
    assert answer.failure.suggestion
    assert answer.table is None
    # The card still shows how the question was understood.
    assert answer.time_range["label"]
    assert [metric.name for metric in answer.metrics] == ["GMV"]


def test_sql_that_never_runs_shows_the_last_attempt_and_why(ask) -> None:
    answer, _ = ask([[call("run_sql", sql=BAD_SQL)], [call("run_sql", sql=BAD_SQL)],
                     text("查不出来")])
    assert answer.status == "error"
    assert answer.failure is not None and answer.failure.kind == "sql_failed"
    assert "试了 2 次" in answer.failure.message
    assert "数据库里没有“amount”这个字段" in answer.failure.message
    assert "amount" in answer.sql
    assert answer.tables_used == ["orders"]


def test_a_model_error_after_a_query_keeps_the_result(ask) -> None:
    answer, _ = ask([[call("run_sql", sql=GOOD_SQL)], BOOM])
    assert answer.status == "partial"
    assert answer.failure is not None and answer.failure.kind == "model_error"
    assert "AuthenticationError" in answer.failure.message
    assert answer.table is not None and answer.table.rows


def test_a_missing_model_is_explained(ask) -> None:
    answer, events = ask(None)
    assert answer.status == "error"
    assert answer.failure is not None and answer.failure.kind == "model_error"
    assert "LLM_API_KEY" in answer.failure.message
    assert events[-1][0] == "answer"


NO_PLAN = text("拆不出来")


def test_an_analysis_timeout_lists_the_queries_done(ask) -> None:
    answer, _ = ask([NO_PLAN, [call("run_sql", sql=GOOD_SQL, purpose="销量排行")], HANG],
                    mode="analysis", analysis_timeout_seconds=0.5)
    assert answer.status == "partial"
    assert answer.mode == "analysis"
    assert answer.failure is not None and answer.failure.kind == "timeout"
    assert [(item.index, item.purpose) for item in answer.results] == [(1, "销量排行")]


def test_unknown_column_is_explained_in_plain_words() -> None:
    message = ("SQL 执行失败：(pymysql.err.OperationalError) (1054, \"Unknown column 'o.cost' "
               "in 'field list'\")\n[SQL: SELECT o.cost FROM safe.orders o]")
    assert explain_sql_error(message) == "数据库里没有“o.cost”这个字段。"


@pytest.mark.parametrize("message, expected", [
    ("SQL 执行失败：(1146, \"Table 'safe.order' doesn't exist\")", "数据库里没有“safe.order”这张表。"),
    ("SQL 执行失败：(3024, 'Query execution was interrupted, maximum statement execution time "
     "exceeded')", "查询超过了数据库的执行时间上限。"),
    ("禁止访问未授权的表：users", "禁止访问未授权的表：users"),
    ("SQL 执行失败：(1064, 'You have an error in your SQL syntax')\n[SQL: SELECT]\n"
     "(Background on this error at: https://sqlalche.me/e/20/f405)",
     "(1064, 'You have an error in your SQL syntax')"),
])
def test_other_sql_errors_keep_the_useful_part(message: str, expected: str) -> None:
    assert explain_sql_error(message) == expected


def test_sql_failure_counts_the_attempts() -> None:
    failure = sql_failure(3, "禁止访问未授权的表：users")
    assert failure.kind == "sql_failed"
    assert failure.message == "SQL 试了 3 次都没有成功。最后一次的原因：禁止访问未授权的表：users"


def test_model_errors_are_told_apart_from_bugs() -> None:
    request = httpx.Request("POST", "https://llm.example/v1")
    assert is_model_error(openai.APITimeoutError(request=request))
    assert is_model_error(httpx.ConnectError("refused"))
    try:
        try:
            raise httpx.ReadTimeout("slow")
        except httpx.ReadTimeout as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert is_model_error(outer)
    assert not is_model_error(KeyError("rows"))
