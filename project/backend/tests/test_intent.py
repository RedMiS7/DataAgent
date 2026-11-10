"""The intent of a question: reading it, checking it, asking back, and how the answer uses it."""

from __future__ import annotations

import itertools
import json
import logging
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from agentscope.message import TextBlock, ToolCallBlock
from app.agent import intent as intents
from app.agent import pipeline, session, tools
from app.agent.intent import (
    DEFAULT_TIME_NOTE,
    MAX_CLARIFY_ROUNDS,
    Catalog,
    Intent,
    check_intent,
    clarify_rounds,
    describe,
    intent_from_dict,
    match_value,
    parse_intent,
    pending_question,
    previous_intent,
    resolve_time,
    settle,
)
from app.agent.pipeline import AskPipeline
from app.agent.scope_model import ScriptedChatModel
from app.api.schemas import HistoryMessage
from app.config import Settings
from app.nl2sql.executor import ExecutionResult
from app.nl2sql.time_parser import parse_time_range
from app.semantic.models import Metric, RetrievalBundle, SchemaItem
from pydantic import ValidationError

AS_OF = date(2026, 10, 31)
CALL_IDS = itertools.count(1)
METRICS = [
    Metric(name="退货率", aliases="退款率,退货", definition="退款金额除以销售额", formula="a/b"),
    Metric(name="退款金额", aliases="退货金额,退货", definition="完成的退款", formula="sum"),
    Metric(name="销售额", aliases="营收,成交额", definition="支付成功的金额", formula="sum"),
]
ITEMS = [
    SchemaItem(table_name="safe.channels", column_name="name", description="渠道名称",
               is_entity=1),
    SchemaItem(table_name="safe.orders", column_name="province", description="收货省份",
               is_entity=1),
    SchemaItem(table_name="safe.orders", column_name="status", description="订单状态"),
]
VALUES = {("safe.channels", "name"): ["抖音", "直播带货", "自然搜索", "小程序"],
          ("safe.orders", "province"): ["河南", "河北", "广东"]}
CATALOG = Catalog(metrics=METRICS, schema_items=ITEMS,
                  entity_values=lambda item: VALUES.get((item.table_name, item.column_name)))


def refund_sql(start: str = "2026-09-01", end: str = "2026-10-01") -> str:
    """A return rate for a period: a ratio, as the checks of plan 2.3 want."""
    return (f"SELECT SUM(refund_amount) / COUNT(*) AS 退货率 FROM safe.refunds "
            f"WHERE completed_at >= '{start}' AND completed_at < '{end}'")


LAST_30_DAYS = ("2026-10-02", "2026-11-01")


def intent(**fields: Any) -> Intent:
    return intent_from_dict({"route": "query", **fields})


def check(**fields: Any) -> intents.Checked:
    return check_intent(intent(**fields), CATALOG, AS_OF)


def kinds(checked: intents.Checked) -> list[str]:
    return [issue.kind for issue in checked.issues]


# Reading the reply

def test_a_full_reply_is_read() -> None:
    read = parse_intent(json.dumps({
        "route": "query", "reason": "要数字",
        "metrics": [{"term": "退款", "name": "退款金额"}],
        "time_range": {"text": "上个月", "needed": True},
        "dimensions": [{"term": "渠道", "field": "channels.name"}],
        "filters": [{"term": "省份", "field": "orders.province", "value": "河南"}],
        "result_shape": {"type": "top_n", "n": "5", "order": "asc"},
        "missing": [], "clarify": {"question": "", "options": []},
    }, ensure_ascii=False))
    assert read.structured
    assert read.metrics[0].name == "退款金额"
    assert read.time_range.text == "上个月"
    assert read.dimensions[0].field == "channels.name"
    assert read.filters[0].value == "河南"
    assert (read.result_shape.type, read.result_shape.n, read.result_shape.order) == (
        "top_n", 5, "asc")


def test_wrong_fields_are_dropped_not_errors() -> None:
    read = intent(metrics=["销售额", 3, {"term": ""}], time_range="昨天",
                  result_shape={"type": "pie", "n": -1, "grain": "hour"},
                  dimensions="渠道", filters=["河南"], missing="时间")
    assert [item.name for item in read.metrics] == ["销售额"]
    assert read.time_range.text == "昨天"
    assert read.result_shape.model_dump() == {"type": "", "n": None, "order": "", "grain": ""}
    assert read.dimensions[0].term == "渠道"
    assert read.filters == []
    assert read.missing == ["时间"]


def test_the_model_question_is_read_from_clarify() -> None:
    read = intent(missing=["要看哪个指标"],
                  clarify={"question": "你想看哪个？", "options": ["a", "b", "c", "d", "e"]})
    assert read.clarify_question == "你想看哪个？"
    assert read.clarify_options == ["a", "b", "c", "d"]


# Checking each field

def test_a_metric_named_by_its_alias_is_brought_to_its_name() -> None:
    checked = check(metrics=[{"term": "营收", "name": "营收"}])
    assert checked.issues == []
    assert checked.intent.metrics[0].name == "销售额"


def test_a_metric_several_metrics_could_be_is_asked_back() -> None:
    checked = check(metrics=[{"term": "退货", "name": "退货率"}])
    assert kinds(checked) == ["metric_ambiguous"]
    assert checked.issues[0].options == ["退货率", "退款金额"]
    # Without asking, the metric the model picked is kept.
    assert checked.intent.metrics[0].name == "退货率"


def test_candidates_the_model_lists_are_asked_back() -> None:
    checked = check(metrics=[{"term": "卖得怎么样", "candidates": ["销售额", "退款金额"]}])
    assert kinds(checked) == ["metric_ambiguous"]
    assert checked.issues[0].options == ["销售额", "退款金额"]


def test_one_known_candidate_is_no_ambiguity() -> None:
    checked = check(metrics=[{"term": "成交", "candidates": ["销售额", "不存在的指标"]}])
    assert checked.issues == []
    assert checked.intent.metrics[0].name == "销售额"


def test_a_metric_outside_the_semantic_layer_is_kept_as_described() -> None:
    checked = check(metrics=[{"term": "下架商品数", "custom": "状态为下架的商品个数"}])
    assert checked.issues == []
    assert checked.intent.metrics[0].custom == "状态为下架的商品个数"
    assert checked.intent.metrics[0].name == ""


def test_a_query_without_a_metric_is_asked_back() -> None:
    checked = check(metrics=[])
    assert kinds(checked) == ["metric_missing"]
    assert checked.issues[0].options == ["退货率", "退款金额", "销售额"]


def test_an_analysis_or_direct_answer_needs_no_metric() -> None:
    assert check_intent(intent(), CATALOG, AS_OF, "analysis").issues == []
    assert check_intent(intent(), CATALOG, AS_OF, "direct").issues == []


@pytest.mark.parametrize("wording, issues", [
    ("上个月", []),
    ("最近7天", []),
    ("", []),
    ("最近", []),
    ("2026年9月", []),
    ("2024-02-01 至 2024-02-29", []),
    ("2026年13月", ["time_unparsed"]),
])
def test_the_time_must_be_one_the_parser_reads(wording: str, issues: list[str]) -> None:
    checked = check(metrics=["销售额"], time_range={"text": wording})
    assert kinds(checked) == issues


def test_a_vague_time_takes_the_default() -> None:
    checked = check(metrics=["销售额"], time_range={"text": "最近"})
    assert checked.intent.time_range.text == ""


def test_a_comparison_the_parser_cannot_read_is_dropped() -> None:
    assert check(metrics=["销售额"], comparison="上上个月").intent.comparison == "上上个月"
    assert check(metrics=["销售额"], comparison="之前").intent.comparison == ""


def test_a_dimension_must_name_a_column() -> None:
    checked = check(metrics=["销售额"], dimensions=[
        {"term": "渠道", "field": "safe.channels.name"}, {"term": "颜色", "field": "products.color"}])
    assert [item.field for item in checked.intent.dimensions] == ["safe.channels.name", ""]
    assert checked.issues == []


@pytest.mark.parametrize("value, matched", [
    ("抖音", "抖音"),
    ("直播", "直播带货"),
    ("抖音渠道", "抖音"),
])
def test_a_filter_value_is_matched_to_a_stored_value(value: str, matched: str) -> None:
    checked = check(metrics=["销售额"],
                    filters=[{"term": "渠道", "field": "channels.name", "value": value}])
    assert checked.issues == []
    assert checked.intent.filters[0].value == matched


def test_a_filter_value_that_is_not_stored_is_asked_back() -> None:
    checked = check(metrics=["销售额"],
                    filters=[{"term": "渠道", "field": "channels.name", "value": "快手"}])
    assert kinds(checked) == ["filter_value"]
    assert "快手" in checked.issues[0].question
    # Nothing is close to 快手, so the stored values are offered.
    assert checked.issues[0].options == ["抖音", "直播带货", "自然搜索", "小程序"]


def test_close_values_are_offered() -> None:
    assert match_value("河南省", ["河南", "河北"]) == ("河南", [])
    assert sorted(match_value("湖南", ["河南", "湖北", "广东"])[1]) == ["河南", "湖北"]
    # A wording several values contain may mean all of them.
    assert match_value("河", ["河南", "河北"]) == ("河", [])


def test_filters_without_stored_values_are_not_checked() -> None:
    unreadable = Catalog(METRICS, ITEMS, entity_values=lambda item: None)
    for checked in (
        check_intent(intent(metrics=["销售额"], filters=[
            {"field": "channels.name", "value": "快手"}]), unreadable, AS_OF),
        check(metrics=["销售额"], filters=[{"field": "orders.status", "value": "已取消"}]),
        check(metrics=["销售额"], filters=[{"field": "orders.color", "value": "红"}]),
    ):
        assert checked.issues == []


def test_top_n_without_a_number_takes_ten() -> None:
    checked = check(metrics=["销售额"], result_shape={"type": "top_n"})
    assert checked.intent.result_shape.n == 10
    assert checked.assumptions == ["未指定数量，按前 10 名"]
    assert checked.issues == []


def test_what_the_model_says_is_missing_is_asked_back() -> None:
    checked = check(metrics=["销售额"], missing=["要和哪个时间比"],
                    clarify={"question": "要和哪个时间比？", "options": ["上个月", "去年"]})
    assert kinds(checked) == ["missing"]
    assert checked.issues[0].options == ["上个月", "去年"]


def test_missing_without_a_question_still_asks() -> None:
    checked = check(missing=["要看哪个指标"])
    # The model already said what is missing, so no second issue for the metric.
    assert kinds(checked) == ["missing"]
    assert "要看哪个指标" in checked.issues[0].question


# Asking back at most twice

def _clarified(times: int) -> list[HistoryMessage]:
    history = [HistoryMessage(role="user", content="退货多少")]
    for index in range(times):
        history.append(HistoryMessage(role="assistant", content=f"反问{index}", status="clarify"))
        history.append(HistoryMessage(role="user", content=f"回答{index}"))
    return history[:-1]


def test_clarify_rounds_count_the_questions_asked_back_in_a_row() -> None:
    assert clarify_rounds([]) == 0
    assert clarify_rounds(_clarified(1)) == 1
    assert clarify_rounds(_clarified(2)) == 2
    answered = [*_clarified(1), HistoryMessage(role="user", content="退货率"),
                HistoryMessage(role="assistant", content="3.9%", status="ok")]
    assert clarify_rounds(answered) == 0


def test_the_pending_question_is_the_one_first_asked() -> None:
    assert pending_question(_clarified(2)) == "退货多少"
    assert pending_question([HistoryMessage(role="user", content="问"),
                             HistoryMessage(role="assistant", content="答", status="ok")]) == ""


def test_settling_takes_the_defaults_and_says_so() -> None:
    checked = check(metrics=[{"term": "退货"}], time_range={"text": "2026年13月"})
    settled = settle(checked)
    assert settled.issues == []
    assert settled.intent.metrics[0].name == "退货率"
    assert settled.intent.time_range.text == ""
    assert settled.assumptions == ["“退货”按“退货率”统计", "没看懂时间“2026年13月”，按最近 30 天统计"]


def test_the_previous_intent_comes_from_the_latest_reply() -> None:
    history = [HistoryMessage(role="user", content="a"),
               HistoryMessage(role="assistant", content="b", intent={"route": "query",
                                                                      "metrics": ["销售额"]}),
               HistoryMessage(role="user", content="c"),
               HistoryMessage(role="assistant", content="d")]
    previous = previous_intent(history)
    assert previous is not None and previous.metrics[0].name == "销售额"


def test_a_history_intent_that_is_too_long_is_rejected() -> None:
    with pytest.raises(ValidationError):
        HistoryMessage(role="assistant", content="", intent={"x": "长" * 5000})


# The period and the line for the user

def test_the_intent_time_is_read_by_the_parser() -> None:
    parsed = parse_time_range("退货率", AS_OF)
    time_range, notes = resolve_time(intent(time_range={"text": "上个月"}), parsed, AS_OF)
    assert (time_range.label, notes) == ("上个月", [])


def test_no_time_says_the_default_is_used() -> None:
    parsed = parse_time_range("退货率", AS_OF)
    _, notes = resolve_time(intent(), parsed, AS_OF)
    assert notes == [DEFAULT_TIME_NOTE]
    _, notes = resolve_time(intent(time_range={"needed": False}), parsed, AS_OF)
    assert notes == []


def test_a_comparison_becomes_the_baseline() -> None:
    parsed = parse_time_range("退货率", AS_OF)
    time_range, _ = resolve_time(intent(time_range={"text": "上个月"}, comparison="去年"),
                                 parsed, AS_OF)
    assert time_range.baseline is not None and time_range.baseline.label == "去年"


@pytest.mark.parametrize("wording, comparison", [
    ("2024年11月", "上月"),
    ("2024年11月", "2024年10月"),
    ("2024年11月相比2024年10月", ""),
])
def test_absolute_intent_dates_resolve_without_clarification(wording: str, comparison: str) -> None:
    checked = check(metrics=["销售额"], time_range={"text": wording}, comparison=comparison)
    assert checked.issues == []
    period, notes = resolve_time(checked.intent, parse_time_range("", AS_OF), AS_OF)
    assert (period.start, period.end) == (date(2024, 11, 1), date(2024, 12, 1))
    assert period.baseline is not None
    assert (period.baseline.start, period.baseline.end) == (date(2024, 10, 1), date(2024, 11, 1))
    assert notes == []


def test_the_understanding_reads_as_one_line() -> None:
    read = intent(metrics=["销售额"], dimensions=[{"term": "渠道"}],
                  filters=[{"field": "orders.province", "value": "河南"}],
                  result_shape={"type": "top_n", "n": 3, "order": "asc"})
    text = describe(read, parse_time_range("上个月", AS_OF))
    assert text == "时间：上个月；指标：销售额；按渠道分组；筛选：河南；结果：前 3 名（从低到高）"
    assert describe(intent(metrics=["渠道数"], time_range={"needed": False}),
                    parse_time_range("", AS_OF)) == "指标：渠道数"


# The pipeline

class RecordingModel(ScriptedChatModel):
    """A scripted model that keeps what it was sent."""

    def __init__(self, script: list[list[Any]]) -> None:
        super().__init__(script)
        self.seen: list[str] = []

    async def _call_api(self, model: str, messages: list | None = None, *args: Any,
                        **kwargs: Any) -> Any:
        self.seen.append(json.dumps(messages, ensure_ascii=False, default=str))
        return await super()._call_api(model, messages, *args, **kwargs)


class StubRetriever:
    def retrieve_with_connection(self, question: str, conn: object,
                                 trace: Any = None) -> RetrievalBundle:
        self.question = question
        return RetrievalBundle()


def call(name: str, **arguments: str) -> ToolCallBlock:
    return ToolCallBlock(type="tool_call", id=f"intent-call-{next(CALL_IDS)}", name=name,
                         input=json.dumps(arguments, ensure_ascii=False))


def agent_script(sql: str | None = None) -> list[list[Any]]:
    # Fresh ids each time: the agent treats a repeated id as the same tool call.
    return [[call("run_sql", sql=sql or refund_sql())], [TextBlock(text="上个月退货率 3.9%。")]]


def reply(**fields: Any) -> str:
    return json.dumps({"route": "query", "reason": "要数字", **fields}, ensure_ascii=False)


@pytest.fixture
def ask(monkeypatch: pytest.MonkeyPatch):
    @contextmanager
    def no_connection(settings: Settings):
        yield None

    executed: list[str] = []
    saved: dict[str, Any] = {}

    def fake_execute(settings: Settings, safe: Any) -> ExecutionResult:
        executed.append(safe.sql)
        return ExecutionResult(columns=["退货率"], rows=[[3.9]], sql=safe.sql)

    def save_log(conn: object, *args: Any, **kwargs: Any) -> int:
        # session_id, question, rewritten, sql, status, ...
        saved["status"] = args[4]
        saved["log"] = kwargs
        return 1

    monkeypatch.setattr(pipeline, "app_connection", no_connection)
    monkeypatch.setattr(pipeline, "load_catalog", lambda settings, conn: CATALOG)
    monkeypatch.setattr(tools, "execute_sql", fake_execute)
    monkeypatch.setattr(session, "save_query_log", save_log)
    monkeypatch.setattr(session, "save_call_traces", lambda *args: None)
    monkeypatch.setattr(session, "save_query_executions", lambda *args: None)

    def run(question: str, router: list[str], agent: list[list[Any]] | None = None,
            history: list[HistoryMessage] | None = None, mode: str = "query"):
        router_model = RecordingModel([[TextBlock(text=item)] for item in router])
        agent_model = RecordingModel(agent or agent_script())
        retriever = StubRetriever()
        events: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(pipeline, "build_router_model", lambda settings: router_model)
        monkeypatch.setattr(pipeline, "build_chat_model", lambda settings: agent_model)
        settings = Settings(_env_file=None, as_of_date=AS_OF)
        answer = AskPipeline(settings, retriever).ask(  # type: ignore[arg-type]
            question, "test", lambda event, data: events.append((event, data)), mode,
            history or [])  # type: ignore[arg-type]
        return answer, SimpleNamespace(router=router_model, agent=agent_model, events=events,
                                       executed=executed, saved=saved, retriever=retriever)

    return run


def test_an_ambiguous_metric_is_asked_back_without_querying(ask) -> None:
    answer, seen = ask("上个月退货多少", [reply(metrics=[{"term": "退货"}],
                                          time_range={"text": "上个月"})])
    assert answer.status == "clarify"
    assert answer.clarify is not None
    assert answer.clarify.options == ["退货率", "退款金额"]
    assert answer.clarify.round == 1
    assert answer.summary == answer.clarify.question
    assert seen.executed == [] and seen.agent.seen == []
    assert seen.saved["status"] == "clarify"
    assert seen.saved["log"]["intent"]["clarify"]["options"] == ["退货率", "退款金额"]


def test_a_question_without_time_is_answered_with_the_default(ask) -> None:
    answer, seen = ask("退货率是多少", [reply(metrics=["退货率"],
                                        result_shape={"type": "ratio"})],
                       agent=agent_script(refund_sql(*LAST_30_DAYS)))
    assert answer.status == "ok"
    assert answer.intent is not None
    assert answer.intent.text == "时间：最近30天；指标：退货率；结果：比率"
    assert answer.intent.assumptions == [DEFAULT_TIME_NOTE]
    assert answer.intent.data["metrics"] == [{"term": "退货率", "name": "退货率"}]
    # The query agent is told how the question was understood.
    assert "对这个问题的理解：时间：最近30天；指标：退货率" in seen.agent.seen[0]
    assert any(data.get("step") == "understood" for _, data in seen.events)
    assert seen.saved["log"]["intent"]["text"] == answer.intent.text


def test_the_intent_time_sets_the_period(ask) -> None:
    answer, _ = ask("上个月退货率", [reply(metrics=["退货率"], time_range={"text": "上个月"})])
    assert answer.time_range["start"] == "2026-09-01"
    assert answer.intent is not None and answer.intent.assumptions == []


def test_after_two_questions_asked_back_the_defaults_apply(ask) -> None:
    ambiguous = reply(metrics=[{"term": "退货"}])
    history = _clarified(MAX_CLARIFY_ROUNDS)
    history.append(HistoryMessage(role="user", content="回答"))
    history.pop()
    answer, seen = ask("随便", [ambiguous], history=history,
                       agent=agent_script(refund_sql(*LAST_30_DAYS)))
    assert answer.status == "ok"
    assert answer.intent is not None
    assert "“退货”按“退货率”统计" in answer.intent.assumptions
    assert seen.executed != []


def test_an_answer_to_a_question_asked_back_goes_with_the_question(ask) -> None:
    history = [HistoryMessage(role="user", content="上个月退货多少"),
               HistoryMessage(role="assistant", content="“退货”可以指几个指标，你想看哪一个？",
                              status="clarify", intent={"route": "query",
                                                        "metrics": [{"term": "退货"}],
                                                        "time_range": {"text": "上个月"}})]
    answer, seen = ask("退货率", [reply(metrics=["退货率"], time_range={"text": "上个月"})],
                       history=history)
    assert answer.status == "ok"
    assert seen.retriever.question == "上个月退货多少 退货率"
    assert answer.time_range["start"] == "2026-09-01"
    assert "上个月退货多少（补充：退货率）" in seen.agent.seen[0]
    # The router reads the earlier understanding and that the last reply asked back.
    assert "【上一轮的理解】" in seen.router.seen[0]
    assert "助手（反问）" in seen.router.seen[0]


def test_a_follow_up_gets_the_previous_intent(ask) -> None:
    history = [HistoryMessage(role="user", content="上个月各渠道的退货率"),
               HistoryMessage(role="assistant", content="抖音最高。", status="ok", mode="query",
                              intent={"route": "query", "metrics": [{"name": "退货率"}],
                                      "dimensions": [{"term": "渠道"}]})]
    answer, seen = ask("那上上个月呢", [reply(metrics=["退货率"], time_range={"text": "上上个月"},
                                         dimensions=[{"term": "渠道"}])], history=history,
                       agent=agent_script(refund_sql("2026-08-01", "2026-09-01")))
    assert answer.status == "ok"
    earlier = seen.router.seen[0].rsplit("【上一轮的理解】（仅供参考", 1)[1]
    assert "dimensions" in earlier and "渠道" in earlier
    assert answer.intent is not None and "按渠道分组" in answer.intent.text


def test_an_unreadable_intent_is_asked_once_more(ask, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app"):
        answer, seen = ask("退货率", ["不是 JSON", reply(metrics=["退货率"])])
    assert seen.router.calls == 2
    assert "只输出一个 JSON 对象" in seen.router.seen[1]
    assert answer.intent is not None
    assert any("重试一次" in item.getMessage() for item in caplog.records)


def test_an_intent_unreadable_twice_runs_the_mode_without_checks(
        ask, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app"):
        answer, seen = ask("退货率", ["不是 JSON", "还不是"], mode="analysis",
                           agent=[[TextBlock(text="不是计划")], *agent_script()])
    assert answer.intent is None
    assert answer.mode == "analysis"
    assert seen.router.calls == 2
    assert any("两次都无法解析，改用分析模式" in item.getMessage() for item in caplog.records)


def test_a_direct_answer_is_not_checked(ask, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AskPipeline, "_answer_directly",
                        lambda self, *args: "退货率是退款金额除以销售额。")
    answer, _ = ask("退货率怎么算", [json.dumps({"route": "direct", "reason": "口径",
                                              "metrics": []})], mode="auto")
    assert answer.status == "text"
    assert answer.intent is not None and answer.intent.text == ""
    assert answer.intent.data["route"] == "direct"


def test_a_user_pick_of_analysis_is_kept_and_needs_no_metric(ask) -> None:
    answer, _ = ask("最近怎么样", [reply(route="direct", metrics=[])], mode="analysis",
                    agent=[[TextBlock(text="不是计划")], *agent_script()])
    assert answer.status != "clarify"
    assert answer.mode == "analysis"


def test_the_analysis_task_carries_the_understanding() -> None:
    settings = Settings(_env_file=None, as_of_date=AS_OF)
    task = AskPipeline(settings, StubRetriever())._analysis_task(  # type: ignore[arg-type]
        "最近怎么样", [], RetrievalBundle(), parse_time_range("最近30天", AS_OF), 4,
        note="[系统信息] 对这个问题的理解：指标：销售额。")
    assert task.time_note.endswith("[系统信息] 对这个问题的理解：指标：销售额。")
