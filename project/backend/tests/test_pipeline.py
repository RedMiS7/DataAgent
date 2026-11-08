from __future__ import annotations

import asyncio
import itertools
import json
import logging
from contextlib import contextmanager
from datetime import date
from typing import Any

import pytest
from agentscope.message import TextBlock, ToolCallBlock
from app.agent import pipeline, session, tools
from app.agent.answer import (
    NO_REPLY,
    fill_missing_replies,
    normalize_citations,
    select_execution,
    strip_citations,
    time_range_warning,
    unverified_numbers,
)
from app.agent.pipeline import (
    AskPipeline,
    build_retrieval_query,
    history_pairs,
    is_followup,
    resolve_time_range,
)
from app.agent.scope_model import ScriptedChatModel
from app.api.schemas import HISTORY_MAX_MESSAGES, AskRequest, HistoryMessage
from app.config import Settings
from app.main import ClosedClientFilter, configure_logging
from app.nl2sql.executor import ExecutionResult, SQLExecutionError
from app.nl2sql.time_parser import parse_time_range
from app.semantic import repository
from app.semantic.models import RetrievalBundle, SchemaItem
from pydantic import ValidationError

AS_OF = date(2026, 10, 31)
CALL_IDS = itertools.count(1)
FIRST_QUESTION = "上个月销量最高的 10 个商品是哪些？"
HISTORY = [("user", FIRST_QUESTION), ("assistant", "上个月销量最高的是童装热销款208号，176件。")]
CLIENT_HISTORY = [HistoryMessage(role=role, content=content,  # type: ignore[arg-type]
                                 mode="query" if role == "assistant" else None)
                  for role, content in HISTORY]
AUGUST_SQL = ("SELECT name, qty FROM safe.products "
              "WHERE day >= '2026-08-01' AND day < '2026-09-01' ORDER BY qty ASC LIMIT 10")
SEPTEMBER_SQL = ("SELECT name, qty FROM safe.products "
                 "WHERE day >= '2026-09-01' AND day < '2026-10-01' ORDER BY qty DESC LIMIT 10")


def result(sql: str, rows: list[list[Any]]) -> ExecutionResult:
    return ExecutionResult(columns=["name", "qty"], rows=rows, sql=sql)


@pytest.mark.parametrize("question, expected", [
    ("那上上个月呢？", True),
    ("再看看退货率", True),
    ("按渠道拆开呢", True),
    ("上上个月哪个商品卖的最差", False),
    ("哪个渠道的 ROI 最低？", False),
])
def test_is_followup(question: str, expected: bool) -> None:
    assert is_followup(question) is expected


def test_retrieval_query_adds_context_only_for_followups() -> None:
    assert build_retrieval_query("那上上个月呢？", HISTORY) == f"{FIRST_QUESTION}；追问：那上上个月呢？"
    assert build_retrieval_query("上上个月哪个商品卖的最差", HISTORY) == "上上个月哪个商品卖的最差"
    assert build_retrieval_query("那上上个月呢？", []) == "那上上个月呢？"


def test_time_comes_from_the_current_question() -> None:
    history = [("user", "上上个月的 GMV"), ("assistant", "好的")]
    assert resolve_time_range("那上个月呢", history, AS_OF).label == "上个月"


def test_followup_without_time_inherits_the_last_named_time() -> None:
    history = [("user", "上个月的 GMV"), ("assistant", "好的"), ("user", "那按渠道呢"),
               ("assistant", "好的")]
    assert resolve_time_range("那按省份呢", history, AS_OF).label == "上个月"


def test_new_question_without_time_keeps_the_default() -> None:
    assert resolve_time_range("一共有几个渠道", HISTORY, AS_OF).is_default


def _span(value: Any) -> tuple[str, str]:
    return value.start.isoformat(), value.end.isoformat()


def test_comparison_takes_the_period_from_history_and_the_baseline_from_the_question() -> None:
    history = [("user", "上个月GMV是多少？"), ("assistant", "上个月GMV是 9665990.5。")]
    result = resolve_time_range("为什么比上上个月高？", history, AS_OF)
    assert _span(result) == ("2026-09-01", "2026-10-01")
    assert result.baseline is not None and _span(result.baseline) == ("2026-08-01", "2026-09-01")


def test_comparison_without_history_uses_the_period_after_the_baseline() -> None:
    result = resolve_time_range("为什么比上个月低？", [], AS_OF)
    assert _span(result) == ("2026-10-01", "2026-11-01")
    assert result.baseline is not None and result.baseline.label == "上个月"


def test_a_named_period_wins_over_history_in_a_comparison() -> None:
    history = [("user", "上上个月的 GMV"), ("assistant", "好的")]
    result = resolve_time_range("本月为什么比上个月低", history, AS_OF)
    assert result.label == "本月至今"
    assert result.baseline is not None and result.baseline.label == "上个月"


def test_a_followup_keeps_the_baseline_of_the_last_comparison() -> None:
    history = [("user", "上个月GMV比上上个月高多少"), ("assistant", "高 12 万。")]
    result = resolve_time_range("那按渠道呢", history, AS_OF)
    assert result.label == "上个月"
    assert result.baseline is not None and result.baseline.label == "上上个月"


def _prompt(question: str, mode: str) -> str:
    settings = Settings(_env_file=None, as_of_date=AS_OF)
    return AskPipeline(settings, StubRetriever())._system_prompt(  # type: ignore[arg-type]
        RetrievalBundle(), parse_time_range(question, AS_OF), mode)  # type: ignore[arg-type]


def test_prompt_names_the_baseline_from_the_question() -> None:
    for mode in ("query", "analysis"):
        prompt = _prompt("上个月GMV为什么比上上个月高", mode)
        assert "对比期：上上个月，[2026-08-01, 2026-09-01)" in prompt
        assert "上一个周期" not in prompt


def test_prompt_without_a_baseline() -> None:
    assert "对比期" not in _prompt("上个月GMV", "query")
    assert "对比期（上一个周期）：[2026-08-01, 2026-09-01)" in _prompt("上个月GMV", "analysis")
    assert "对比期（上一个周期）：[2026-10-18, 2026-10-25)" in _prompt("最近7天GMV", "analysis")


def test_user_content_carries_the_baseline_dates() -> None:
    settings = Settings(_env_file=None, as_of_date=AS_OF)
    content = AskPipeline(settings, StubRetriever())._user_content(  # type: ignore[arg-type]
        "上个月比上上个月高多少", parse_time_range("上个月比上上个月高多少", AS_OF))
    assert "对比期 上上个月：日期 >= '2026-08-01' 且 < '2026-09-01'" in content


def test_select_execution_prefers_the_query_behind_the_conclusion() -> None:
    answer = result(AUGUST_SQL, [["母婴热销款145号", 63]])
    extra = result(SEPTEMBER_SQL, [["童装热销款208号", 176]])
    assert select_execution([answer, extra], "销量最差的是母婴热销款145号，仅63件。") is answer


def test_select_execution_uses_the_latest_match_after_a_correction() -> None:
    wrong = result(AUGUST_SQL, [["甲", 63]])
    fixed = result(SEPTEMBER_SQL, [["乙", 63]])
    assert select_execution([wrong, fixed], "销量是63件。") is fixed


def test_select_execution_falls_back_to_the_first_result_with_rows() -> None:
    empty = result(AUGUST_SQL, [])
    first = result(SEPTEMBER_SQL, [["乙", 1]])
    second = result(SEPTEMBER_SQL, [["丙", 2]])
    assert select_execution([empty, first, second], "没有数字的结论") is first
    assert select_execution([empty], "") is empty
    assert select_execution([], "") is None


@pytest.mark.parametrize("sql, warns", [
    ("WHERE d >= '2026-09-01' AND d < '2026-10-01'", False),
    ("WHERE d BETWEEN '2026-09-01' AND '2026-09-30'", False),
    ("WHERE d >= '2026-09-01 00:00:00' AND d < '2026-10-01 00:00:00'", False),
    ("SELECT COUNT(*) FROM safe.channels", False),
    ("WHERE d >= '2026-10-01' AND d < '2026-11-01'", True),
])
def test_time_range_warning(sql: str, warns: bool) -> None:
    time_range = parse_time_range("上个月", AS_OF)
    assert bool(time_range_warning(sql, time_range)) is warns


class StubRetriever:
    def retrieve_with_connection(self, question: str, conn: object) -> RetrievalBundle:
        self.question = question
        return RetrievalBundle()


@pytest.fixture
def ask(monkeypatch: pytest.MonkeyPatch):
    """Runs the real pipeline and Agent loop with a scripted model and no database."""
    @contextmanager
    def no_connection(settings: Settings):
        yield None

    rows = {"2026-08-01": [["母婴热销款145号", 63]], "2026-09-01": [["童装热销款208号", 176]]}

    def fake_execute(settings: Settings, safe: Any) -> ExecutionResult:
        key = next(day for day in rows if day in safe.sql)
        return result(safe.sql, rows[key])

    monkeypatch.setattr(pipeline, "app_connection", no_connection)
    monkeypatch.setattr(tools, "execute_sql", fake_execute)
    monkeypatch.setattr(session, "save_query_log", lambda *args: 1)
    monkeypatch.setattr(session, "save_call_traces", lambda *args: None)

    def run(question: str, script: list[list[Any]], mode: str = "query",
            history: list[HistoryMessage] | None = None, emit: Any = None,
            route: list[Any] | None = None):
        # The first model built goes to the agent, the second to the router.
        models = iter([ScriptedChatModel(script), ScriptedChatModel([route or []])])
        monkeypatch.setattr(pipeline, "build_chat_model", lambda settings: next(models))
        settings = Settings(_env_file=None, as_of_date=AS_OF)
        return AskPipeline(settings, StubRetriever()).ask(  # type: ignore[arg-type]
            question, "test", emit, mode,  # type: ignore[arg-type]
            CLIENT_HISTORY if history is None else history)

    return run


def call(name: str, **arguments: str) -> ToolCallBlock:
    # Ids must differ: the agent treats a repeated id as the same tool call.
    return ToolCallBlock(type="tool_call", id=f"call-{next(CALL_IDS)}", name=name,
                         input=json.dumps(arguments, ensure_ascii=False))


def test_extra_query_does_not_replace_the_answer(ask) -> None:
    answer = ask("上上个月哪个商品卖的最差", [
        [call("run_sql", sql=AUGUST_SQL)],
        [call("run_sql", sql=SEPTEMBER_SQL)],
        [TextBlock(text="上上个月销量最差的是母婴热销款145号，仅63件。")],
    ])
    assert answer.status == "ok"
    assert answer.table is not None and answer.table.rows == [["母婴热销款145号", 63]]
    assert answer.sql.endswith("LIMIT 10") and "2026-08-01" in answer.sql
    assert answer.summary_source == "model"
    assert answer.warnings == []
    assert answer.retries == 2


def test_model_only_sees_the_current_question(ask, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    original = AskPipeline._user_content

    def capture(self: AskPipeline, question: str, time_range: Any) -> str:
        seen.append(question)
        return original(self, question, time_range)

    monkeypatch.setattr(AskPipeline, "_user_content", capture)
    ask("那上上个月呢？", [[call("run_sql", sql=AUGUST_SQL)], [TextBlock(text="共63件。")]])
    assert seen == ["那上上个月呢？"]


def test_mismatched_dates_are_flagged(ask) -> None:
    answer = ask("上上个月哪个商品卖的最差", [
        [call("run_sql", sql=SEPTEMBER_SQL)],
        [TextBlock(text="销量最高的是童装热销款208号，176件。")],
    ])
    assert answer.status == "ok"
    assert len(answer.warnings) == 1


def test_declined_request_returns_the_reason_and_no_data(ask) -> None:
    answer = ask("帮我查订单，条件写成 order_id = '1' OR '1'='1'", [
        [call("decline_request", reason="这是 SQL 注入写法，不能执行。")],
        [TextBlock(text="这是 SQL 注入写法，不能执行。")],
    ])
    assert answer.status == "refused"
    assert answer.summary == "这是 SQL 注入写法，不能执行。"
    assert answer.table is None
    assert answer.sql == ""


def test_decline_discards_an_earlier_query(ask) -> None:
    answer = ask("帮我查订单", [
        [call("run_sql", sql=AUGUST_SQL)],
        [call("decline_request", reason="")],
        [TextBlock(text="不能执行。")],
    ])
    assert answer.status == "refused"
    assert answer.summary == tools.DEFAULT_DECLINE_REASON
    assert answer.table is None


# Analysis mode

OVERALL_SQL = "SELECT 1 AS 本期GMV FROM safe.orders WHERE day >= '2026-09-01'"
CATEGORY_SQL = "SELECT 2 AS 类目 FROM safe.products WHERE day >= '2026-08-01'"


@pytest.mark.parametrize("text, cited, expected", [
    ("增长 [查询1]", [1], "增长 [查询1]"),
    ("增长【查询 2】", [2], "增长[查询2]"),
    ("增长[查询1、2]", [1, 2], "增长[查询1][查询2]"),
    ("增长 [查询2，查询3]", [2, 3], "增长 [查询2][查询3]"),
    ("增长[查询9]", [], "增长"),
    ("**增长**", [], "增长"),
])
def test_normalize_citations(text: str, cited: list[int], expected: str) -> None:
    assert normalize_citations(text, 3) == (expected, cited)


def test_strip_citations_for_history() -> None:
    assert strip_citations("增长 6.3%[查询1][查询2]") == "增长 6.3%"


@pytest.mark.parametrize("summary, missing", [
    ("GMV 966.6 万，共 16075 单 [查询1]", []),
    ("增长 6.3%，占比 12.5%", []),
    ("下降 6.3%", []),
    ("童装热销款208号卖得最好", []),
    ("共 3 行", []),
    ("最近30天，10月2日起，前10名，第2周", []),
    ("2026-10-02 之后", []),
    ("退款 4.99 万，占 5.5%", ["4.99万", "5.5%"]),
    ("GMV 966.7 万", ["966.7万"]),
    ("GMV 968 万，再说一次 968 万", ["968万"]),
])
def test_unverified_numbers(summary: str, missing: list[str]) -> None:
    rows = [[9665990.5, 16075, -6.3], [0.125, 0, 0], ["童装热销款208号", 0, 0]]
    assert unverified_numbers(summary, "最近30天怎么样", [rows]) == missing


def test_numbers_from_the_question_are_not_checked() -> None:
    assert unverified_numbers("按 37 这个门槛看", "门槛 37 够吗", [[[1]]]) == []


def test_numbers_are_checked_against_every_given_query() -> None:
    assert unverified_numbers("1234 和 5678", "问题", [[[1234]], [[5678]]]) == []


NO_PLAN = [TextBlock(text="不是计划")]


@pytest.fixture
def analyze(monkeypatch: pytest.MonkeyPatch):
    """Runs analysis mode without a plan, through the real Agent loop, recording what would be stored."""
    @contextmanager
    def no_connection(settings: Settings):
        yield None

    saved: dict[str, Any] = {"events": []}

    def fake_execute(settings: Settings, safe: Any) -> ExecutionResult:
        if "2026-08-01" in safe.sql:
            return ExecutionResult(columns=["类目", "销量"], rows=[["鞋靴", 200], ["童装", 150]],
                                   sql=safe.sql)
        return ExecutionResult(columns=["本期GMV"], rows=[[9665990.5]], sql=safe.sql)

    def save_log(conn: object, session_id: str, question: str, rewritten: str, sql: str,
                 *rest: Any) -> int:
        saved["log_sql"] = sql
        return 1

    monkeypatch.setattr(pipeline, "app_connection", no_connection)
    monkeypatch.setattr(tools, "execute_sql", fake_execute)
    monkeypatch.setattr(session, "save_query_log", save_log)
    monkeypatch.setattr(session, "save_call_traces", lambda *args: None)

    def run(script: list[list[Any]], mode: str = "analysis"):
        # The planner reads the first reply; an unreadable plan runs the single agent.
        if mode == "analysis":
            script = [NO_PLAN, *script]
        monkeypatch.setattr(pipeline, "build_chat_model", lambda settings: ScriptedChatModel(script))
        settings = Settings(_env_file=None, as_of_date=AS_OF)
        answer = AskPipeline(settings, StubRetriever()).ask(  # type: ignore[arg-type]
            "分析一下最近30天的销量", "test",
            lambda event, data: saved["events"].append((event, data)), mode)  # type: ignore[arg-type]
        return answer, saved

    return run


def test_analysis_shows_only_the_cited_queries(analyze) -> None:
    answer, _ = analyze([
        [call("run_sql", sql=OVERALL_SQL, purpose="整体")],
        [call("run_sql", sql=OVERALL_SQL, purpose="没用上的查询")],
        [call("run_sql", sql=CATEGORY_SQL, purpose="类目拆分")],
        [TextBlock(text="整体不错。\n- GMV 966.6 万 [查询1]\n- 鞋靴销量 200 [查询3]\n- 建议：加投放")],
    ])
    assert answer.status == "ok"
    assert answer.mode == "analysis"
    assert answer.summary_source == "model"
    assert [(item.index, item.purpose) for item in answer.results] == [(1, "整体"), (3, "类目拆分")]
    assert answer.warnings == []
    assert answer.table is not None and answer.table.rows == [[9665990.5]]
    assert answer.sql == answer.results[0].sql


def test_analysis_shows_every_query_without_citations(analyze) -> None:
    answer, _ = analyze([
        [call("run_sql", sql=OVERALL_SQL)],
        [call("run_sql", sql=CATEGORY_SQL)],
        [TextBlock(text="整体平稳，没有明显变化。")],
    ])
    assert [item.index for item in answer.results] == [1, 2]
    assert answer.results[0].purpose == "查询 1"


def test_analysis_keeps_the_conclusion_and_flags_unknown_numbers(analyze) -> None:
    text = "整体增长。\n- GMV 966.6 万，增长 6.3% [查询1]"
    answer, _ = analyze([[call("run_sql", sql=OVERALL_SQL)], [TextBlock(text=text)]])
    assert answer.summary == text
    assert answer.summary_source == "model"
    assert answer.warnings == ["这些数字未能在查询结果中核对：6.3%"]


def test_analysis_keeps_sql_out_of_the_query_log(analyze) -> None:
    _, saved = analyze([[call("run_sql", sql=OVERALL_SQL)], [TextBlock(text="整体平稳。[查询1]")]])
    assert saved["log_sql"] == ""


def test_query_mode_still_logs_its_sql(analyze) -> None:
    answer, saved = analyze([[call("run_sql", sql=OVERALL_SQL)], [TextBlock(text="GMV 9665990.5。")]],
                            mode="query")
    assert saved["log_sql"] == answer.sql != ""
    assert answer.mode == "query"
    assert answer.results == []


def test_analysis_stops_running_queries_at_the_limit(analyze) -> None:
    script = [[call("run_sql", sql=OVERALL_SQL)] for _ in range(tools.ANALYSIS_MAX_QUERIES + 1)]
    answer, _ = analyze([*script, [TextBlock(text="整体平稳。")]])
    assert len(answer.results) == tools.ANALYSIS_MAX_QUERIES
    assert answer.retries == tools.ANALYSIS_MAX_QUERIES
    assert any("查询次数已用完" in warning for warning in answer.warnings)


def test_analysis_lists_each_query_as_a_step(analyze) -> None:
    _, saved = analyze([[call("run_sql", sql=CATEGORY_SQL, purpose="类目拆分")],
                        [TextBlock(text="整体平稳。")]])
    steps = [data for event, data in saved["events"] if event == "status"]
    assert {"step": "query_1", "message": "查询 1：类目拆分，返回 2 行"} in steps
    assert not any(step["step"] in ("validating", "executing") for step in steps)


def test_analysis_prompt_differs_only_in_the_answer_rule() -> None:
    assert pipeline.QUERY_ANSWER_RULE in pipeline.SYSTEM_PROMPT
    assert pipeline.QUERY_ANSWER_RULE not in pipeline.ANALYSIS_PROMPT
    assert pipeline.ANALYSIS_PROMPT.replace(pipeline.ANALYSIS_ANSWER_RULE,
                                            pipeline.QUERY_ANSWER_RULE) == pipeline.SYSTEM_PROMPT


# History from the client

def test_history_pairs_drop_citations_from_replies() -> None:
    history = [HistoryMessage(role="user", content="分析销量[查询1]"),
               HistoryMessage(role="assistant", content="增长 6.3%[查询1][查询2]", mode="analysis")]
    assert history_pairs(history) == [("user", "分析销量[查询1]"), ("assistant", "增长 6.3%")]


def test_the_model_sees_the_history_the_client_sent(ask, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[tuple[str, str]]] = []
    original = pipeline._history_messages

    def capture(turns: list[tuple[str, str]]) -> Any:
        seen.append(turns)
        return original(turns)

    monkeypatch.setattr(pipeline, "_history_messages", capture)
    history = [HistoryMessage(role="user" if index % 2 == 0 else "assistant",
                              content=f"消息{index}") for index in range(30)]
    ask("上上个月哪个商品卖的最差", [[call("run_sql", sql=AUGUST_SQL)],
                                       [TextBlock(text="销量最差的是母婴热销款145号，仅63件。")]],
        history=history)
    assert seen == [[("user" if index % 2 == 0 else "assistant", f"消息{index}")
                     for index in range(30)]]


def test_a_question_without_a_reply_gets_a_placeholder(ask, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[tuple[str, str]]] = []
    original = pipeline._history_messages

    def capture(turns: list[tuple[str, str]]) -> Any:
        seen.append(turns)
        return original(turns)

    monkeypatch.setattr(pipeline, "_history_messages", capture)
    # An older client sent only the question of a turn that failed.
    ask("上上个月哪个商品卖的最差", [[call("run_sql", sql=AUGUST_SQL)],
                                       [TextBlock(text="销量最差的是母婴热销款145号，仅63件。")]],
        history=[HistoryMessage(role="user", content="上个月各省份的退款金额是多少？")])
    assert seen == [[("user", "上个月各省份的退款金额是多少？"), ("assistant", NO_REPLY)]]


def test_questions_in_a_row_each_get_a_reply() -> None:
    history = [HistoryMessage(role="user", content="甲"), HistoryMessage(role="user", content="乙"),
               HistoryMessage(role="assistant", content="答乙", mode="query")]
    assert [(item.role, item.content) for item in fill_missing_replies(history)] == [
        ("user", "甲"), ("assistant", NO_REPLY), ("user", "乙"), ("assistant", "答乙")]
    assert fill_missing_replies([]) == []


def test_a_failed_or_stopped_reply_says_it_found_nothing() -> None:
    history = [
        HistoryMessage(role="user", content="甲"),
        HistoryMessage(role="assistant", content="SQL 试了 3 次都没有成功。", status="error",
                       mode="query"),
        HistoryMessage(role="user", content="乙"),
        HistoryMessage(role="assistant", content="", status="stopped"),
        HistoryMessage(role="user", content="丙"),
        HistoryMessage(role="assistant", content="", status="error"),
        HistoryMessage(role="user", content="丁"),
        HistoryMessage(role="assistant", content="只查到一部分 [查询1]", status="partial"),
    ]
    assert history_pairs(history) == [
        ("user", "甲"), ("assistant", "（这一轮没有查到结果：SQL 试了 3 次都没有成功。）"),
        ("user", "乙"), ("assistant", "（用户中途停止了这一轮，没有回答）"),
        ("user", "丙"), ("assistant", "（这一轮没有查到结果）"),
        ("user", "丁"), ("assistant", "只查到一部分"),
    ]


def test_a_followup_reads_the_metric_from_the_client_history(ask) -> None:
    retriever_question: list[str] = []

    class Recording(StubRetriever):
        def retrieve_with_connection(self, question: str, conn: object) -> RetrievalBundle:
            retriever_question.append(question)
            return RetrievalBundle()

    pipeline_obj = AskPipeline(Settings(_env_file=None, as_of_date=AS_OF), Recording())  # type: ignore[arg-type]
    pipeline_obj.ask("那上上个月呢？", "test", history=CLIENT_HISTORY)
    assert retriever_question == [f"{FIRST_QUESTION}；追问：那上上个月呢？"]


def test_comparison_followup_compares_september_with_august(ask,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []
    original = AskPipeline._system_prompt

    def capture(self: AskPipeline, bundle: Any, time_range: Any, mode: Any = "query") -> str:
        prompts.append(original(self, bundle, time_range, mode))
        return prompts[-1]

    monkeypatch.setattr(AskPipeline, "_system_prompt", capture)
    events: list[tuple[str, dict[str, Any]]] = []
    history = [HistoryMessage(role="user", content="上个月GMV是多少？"),
               HistoryMessage(role="assistant", content="上个月GMV是 9665990.5。", mode="query")]
    answer = ask("为什么比上上个月高？",
                 [NO_PLAN, [call("run_sql", sql=SEPTEMBER_SQL)],
                  [TextBlock(text="本期更高。[查询1]")]],
                 mode="analysis", history=history,
                 emit=lambda event, data: events.append((event, data)))
    assert answer.time_range == {"start": "2026-09-01", "end_exclusive": "2026-10-01",
                                 "label": "上个月"}
    assert "对比期：上上个月，[2026-08-01, 2026-09-01)" in prompts[0]
    parsed = next(data for event, data in events if data.get("step") == "time_parsed")
    assert parsed["message"] == ("时间范围：上个月（2026-09-01 至 2026-10-01，不含右端）；"
                                 "对比期：上上个月（2026-08-01 至 2026-09-01，不含右端）")


def test_ask_request_limits_the_history() -> None:
    message = {"role": "user", "content": "问题"}
    AskRequest(question="问题", history=[message] * HISTORY_MAX_MESSAGES)
    with pytest.raises(ValidationError):
        AskRequest(question="问题", history=[message] * (HISTORY_MAX_MESSAGES + 1))


def test_ask_request_rejects_an_unknown_mode() -> None:
    assert AskRequest(question="问题", mode="auto").mode == "auto"
    with pytest.raises(ValidationError):
        AskRequest(question="问题", mode="plan")


# Auto mode

ROUTE_ANALYSIS = [TextBlock(text='{"route": "analysis", "reason": "问整体经营情况"}')]
ROUTE_QUERY = [TextBlock(text='{"route": "query", "reason": "要一个具体数字"}')]


def test_auto_mode_runs_the_mode_the_router_chose(ask) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    answer = ask("最近生意怎么样？", [
        NO_PLAN,
        [call("run_sql", sql=AUGUST_SQL, purpose="整体")],
        [TextBlock(text="整体平稳。\n- 母婴热销款145号 63 件 [查询1]\n- 建议：观察")],
    ], mode="auto", emit=lambda event, data: events.append((event, data)), route=ROUTE_ANALYSIS)
    assert answer.mode == "analysis"
    assert answer.status == "ok"
    assert answer.route is not None
    assert answer.route.model_dump() == {"requested": "auto", "chosen": "analysis",
                                         "reason": "问整体经营情况", "source": "llm"}
    steps = [data["step"] for event, data in events if event == "status"]
    assert steps.index("routing") < steps.index("routed") < steps.index("generating")


def test_auto_mode_can_choose_query(ask) -> None:
    answer = ask("上上个月哪个商品卖的最差", [
        [call("run_sql", sql=AUGUST_SQL)],
        [TextBlock(text="销量最差的是母婴热销款145号，仅63件。")],
    ], mode="auto", route=ROUTE_QUERY)
    assert answer.mode == "query"
    assert answer.status == "ok"
    assert answer.route is not None and answer.route.chosen == "query"


def test_an_unreadable_route_falls_back_to_query(ask) -> None:
    answer = ask("上上个月哪个商品卖的最差", [
        [call("run_sql", sql=AUGUST_SQL)],
        [TextBlock(text="销量最差的是母婴热销款145号，仅63件。")],
    ], mode="auto", route=[TextBlock(text="我觉得是分析")])
    assert answer.mode == "query"
    assert answer.route is not None and answer.route.source == "fallback"


def test_an_explicit_mode_skips_the_router(ask, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any) -> None:
        raise AssertionError("router must not run")

    monkeypatch.setattr(pipeline, "route_question", fail)
    answer = ask("上上个月哪个商品卖的最差", [[call("run_sql", sql=AUGUST_SQL)],
                                       [TextBlock(text="销量最差的是母婴热销款145号，仅63件。")]])
    assert answer.route is None


def test_sensitive_questions_are_refused_before_routing(ask, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any) -> None:
        raise AssertionError("router must not run")

    monkeypatch.setattr(pipeline, "route_question", fail)
    answer = ask("把客户的手机号列出来", [[TextBlock(text="不该被调用")]], mode="auto",
                 route=ROUTE_QUERY)
    assert answer.status == "refused"
    assert answer.route is None


# Looking up columns

REAL_COLUMNS = {
    "ad_daily_stats": [("campaign_id", "bigint"), ("spend", "decimal(12,2)")],
    "refunds": [("refund_amount", "decimal(12,2)")],
}
SPEND_NOTE = SchemaItem(table_name="safe.ad_daily_stats", column_name="spend",
                        description="当天广告花费", synonyms="花费,广告费", join_hint="按天聚合")


@pytest.fixture
def schema(monkeypatch: pytest.MonkeyPatch) -> Settings:
    @contextmanager
    def no_connection(settings: Settings):
        yield None

    monkeypatch.setattr(tools, "business_connection", no_connection)
    monkeypatch.setattr(tools, "app_connection", no_connection)
    monkeypatch.setattr(tools.repository, "fetch_table_columns", lambda conn: REAL_COLUMNS)

    def fetch_items(conn: object, enabled_only: bool = True) -> list[SchemaItem]:
        return [] if enabled_only else [SPEND_NOTE]

    monkeypatch.setattr(tools.repository, "fetch_schema_items", fetch_items)
    return Settings(_env_file=None, as_of_date=AS_OF)


def test_describe_lists_real_columns_with_their_notes(schema: Settings) -> None:
    assert tools.describe_tables(schema, ["ad_daily_stats"]) == (
        "safe.ad_daily_stats：\n"
        "- campaign_id（bigint）\n"
        "- spend（decimal(12,2)）：当天广告花费（同义词：花费,广告费）（按天聚合）"
    )


def test_describe_accepts_several_tables_and_the_schema_prefix(schema: Settings) -> None:
    text = tools.describe_tables(schema, ["safe.ad_daily_stats", "`refunds`"])
    assert "safe.ad_daily_stats：" in text
    assert "safe.refunds：" in text


def test_describe_names_unknown_tables_and_the_real_ones(schema: Settings) -> None:
    text = tools.describe_tables(schema, ["refunds", "mysql.user"])
    assert "refund_amount" in text
    assert "没有这些表：mysql.user。可用的表：ad_daily_stats、refunds" in text


def test_every_seeded_column_note_matches_the_schema_file() -> None:
    items = repository.load_seed(Settings(_env_file=None).seed_dir).schema_items
    keys = [(item.table_name, item.column_name) for item in items]
    assert len(keys) == len(set(keys)) == 69
    assert all(item.description for item in items)


def test_keys_and_timestamps_stay_out_of_retrieval() -> None:
    items = repository.load_seed(Settings(_env_file=None).seed_dir).schema_items
    hidden = {(item.table_name, item.column_name) for item in items if not item.enabled}
    assert ("safe.ad_daily_stats", "id") in hidden
    assert ("safe.customers", "name") in hidden
    assert ("safe.orders", "city") not in hidden
    assert ("safe.orders", "id") not in hidden


def tool_names(mode: str) -> set[str]:
    settings = Settings(_env_file=None, as_of_date=AS_OF)
    context = tools.AgentRunContext(settings=settings, retriever=None, mode=mode)  # type: ignore[arg-type]
    schemas = asyncio.run(tools.build_toolkit(context).get_tool_schemas())
    return {item["function"]["name"] for item in schemas}


@pytest.mark.parametrize("mode", ["query", "analysis"])
def test_both_modes_can_look_up_columns(mode: str) -> None:
    assert "describe_table" in tool_names(mode)


def test_both_prompts_forbid_guessing_columns() -> None:
    assert "不要按常见叫法猜" in pipeline.SYSTEM_PROMPT
    assert "不要按常见叫法猜" in pipeline.ANALYSIS_PROMPT


def test_analysis_agent_can_call_describe_table(analyze, schema: Settings) -> None:
    answer, saved = analyze([
        [call_with("describe_table", tables=["ad_daily_stats", "refunds"])],
        [call("run_sql", sql=OVERALL_SQL)],
        [TextBlock(text="整体平稳。[查询1]")],
    ])
    assert answer.status == "ok"
    assert {"step": "describing", "message": "查看字段：ad_daily_stats、refunds"} in [
        data for event, data in saved["events"] if event == "status"]


def call_with(name: str, **arguments: Any) -> ToolCallBlock:
    return ToolCallBlock(type="tool_call", id=f"call-{next(CALL_IDS)}", name=name,
                         input=json.dumps(arguments, ensure_ascii=False))


def failing_sql(monkeypatch: pytest.MonkeyPatch) -> tools.AgentRunContext:
    class DriverError(Exception):
        orig = "(1054, \"Unknown column 'cost' in 'field list'\")"

    def fail(settings: Settings, safe: Any) -> ExecutionResult:
        error = SQLExecutionError("SQL 执行失败：Unknown column 'cost' [SQL: 很长的语句]", safe.sql)
        error.__cause__ = DriverError()
        raise error

    monkeypatch.setattr(tools, "execute_sql", fail)
    settings = Settings(_env_file=None, as_of_date=AS_OF)
    return tools.AgentRunContext(settings=settings, retriever=None, log_tag="1f2c07")  # type: ignore[arg-type]


BAD_SQL = "SELECT cost\n  FROM safe.ad_daily_stats"


@pytest.mark.parametrize("mode", ["query", "analysis"])
def test_unknown_column_points_to_describe_table(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    context = failing_sql(monkeypatch)
    context.mode = mode
    assert tools.UNKNOWN_COLUMN_HINT in asyncio.run(tools._make_run_sql(context)(BAD_SQL))


def test_sql_failure_is_reported_once(monkeypatch: pytest.MonkeyPatch) -> None:
    context = failing_sql(monkeypatch)
    events: list[dict[str, Any]] = []
    context.emit = lambda event, data: events.append(data)
    reply = asyncio.run(tools._make_run_sql(context)(BAD_SQL))
    assert reply.count("SQL 执行失败") == 1
    assert events[-1]["message"].count("SQL 执行失败") == 1


# Logging

def test_sql_failure_logs_the_database_error_and_the_statement(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    context = failing_sql(monkeypatch)
    with caplog.at_level(logging.INFO, logger="app"):
        asyncio.run(tools._make_run_sql(context)(BAD_SQL))
    record = next(item for item in caplog.records if item.levelno == logging.WARNING)
    line = record.getMessage()
    assert line.startswith("[1f2c07] 查询 1 失败")
    assert "(1054, \"Unknown column 'cost' in 'field list'\")" in line
    assert "sql=SELECT cost FROM safe.ad_daily_stats" in line
    assert "很长的语句" not in line


def test_blocked_sql_is_logged(monkeypatch: pytest.MonkeyPatch,
                               caplog: pytest.LogCaptureFixture) -> None:
    context = failing_sql(monkeypatch)
    with caplog.at_level(logging.INFO, logger="app"):
        asyncio.run(tools._make_run_sql(context)("DELETE FROM safe.orders"))
    line = next(item for item in caplog.records if item.levelno == logging.WARNING).getMessage()
    assert "被拦截" in line
    assert "sql=DELETE FROM safe.orders" in line


def test_a_question_logs_its_start_and_end(analyze, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app"):
        analyze([[call("run_sql", sql=OVERALL_SQL, purpose="整体")],
                 [TextBlock(text="整体平稳。[查询1]")]])
    lines = [item.getMessage() for item in caplog.records]
    assert any("问答开始 mode=analysis" in line for line in lines)
    assert any("查询 1 成功" in line and "1 行 目的=整体" in line for line in lines)
    assert any("问答结束 status=ok" in line for line in lines)


def test_agent_failure_logs_the_traceback(analyze, monkeypatch: pytest.MonkeyPatch,
                                          caplog: pytest.LogCaptureFixture) -> None:
    async def boom(self: Any, inputs: Any) -> None:
        raise RuntimeError("模型不可用")

    monkeypatch.setattr(pipeline.Agent, "reply", boom)
    with caplog.at_level(logging.INFO, logger="app"):
        answer, _ = analyze([])
    assert answer.status == "error"
    failure = next(item for item in caplog.records if "Agent 执行失败" in item.getMessage())
    assert failure.exc_info is not None
    assert any("问答失败 status=error" in item.getMessage() for item in caplog.records)


def noise(message: str) -> logging.LogRecord:
    return logging.LogRecord("asyncio", logging.ERROR, __file__, 1, message, None, None)


def test_only_the_client_cleanup_error_is_filtered() -> None:
    keep = ClosedClientFilter()
    assert not keep.filter(noise("never retrieved\nfuture: <Task coro=<AsyncClient.aclose()>>"))
    assert keep.filter(noise("never retrieved\nfuture: <Task coro=<other()>>"))


def test_log_level_applies_to_the_app_only() -> None:
    app_logger = logging.getLogger("app")
    before = app_logger.level
    try:
        configure_logging("debug")
        assert app_logger.level == logging.DEBUG
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    finally:
        app_logger.setLevel(before)


# Return-rate attribution

def test_attribution_compares_with_the_baseline_or_the_previous_month(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from app.analysis import attribution

    def fake_run(settings: Settings, sql: str) -> list[list[Any]]:
        return [[100, 80, 1000, 1000]] if "AS prev_gmv" in sql else []

    monkeypatch.setattr(attribution, "_run", fake_run)
    monkeypatch.setattr(attribution, "build_chat_model", lambda settings: None)
    settings = Settings(_env_file=None, as_of_date=AS_OF)
    named = attribution.attribute_return_rate(settings, "上个月退货率为什么比去年高？")
    plain = attribution.attribute_return_rate(settings, "上个月退货率为什么上升？")
    assert named.current["start"] == plain.current["start"] == "2026-09-01"
    assert (named.previous["start"], named.previous["end_exclusive"]) == ("2025-01-01", "2026-01-01")
    assert "completed_at >= '2025-01-01'" in named.sql[0]
    assert plain.previous == {"start": "2026-08-01", "end_exclusive": "2026-09-01",
                              "label": "2026-08-01 至 2026-08-31"}


# Figures quoted without their citation

def _two_products(ask, conclusion: str):
    return ask("最近哪些商品值得推？", [
        NO_PLAN,
        [call("run_sql", sql=AUGUST_SQL, purpose="八月商品")],
        [call("run_sql", sql=SEPTEMBER_SQL, purpose="九月商品")],
        [TextBlock(text=conclusion)],
    ], mode="analysis", history=[])


def test_a_product_from_an_uncited_query_is_verified_and_shown(ask) -> None:
    answer = _two_products(ask, "童装更强。\n- 母婴热销款145号 63 件 [查询1]\n- 建议：加推童装208号，176 件")
    assert answer.warnings == []
    assert [item.index for item in answer.results] == [1, 2]


def test_a_product_no_query_returned_is_still_flagged(ask) -> None:
    answer = _two_products(ask, "童装更强。\n- 母婴热销款145号 63 件 [查询1]\n- 建议：加推鞋靴999号")
    assert answer.warnings == ["这些数字未能在查询结果中核对：999"]
    assert [item.index for item in answer.results] == [1]
