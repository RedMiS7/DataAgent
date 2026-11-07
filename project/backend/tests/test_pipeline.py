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
    normalize_citations,
    select_execution,
    strip_citations,
    time_range_warning,
    unverified_numbers,
)
from app.agent.pipeline import (
    AskPipeline,
    build_retrieval_query,
    is_followup,
    resolve_time_range,
)
from app.agent.scope_model import ScriptedChatModel
from app.api.schemas import AskRequest
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
    monkeypatch.setattr(session, "ensure_conversation", lambda *args: None)
    monkeypatch.setattr(session, "load_history", lambda *args: HISTORY)
    monkeypatch.setattr(session, "save_message", lambda *args: None)
    monkeypatch.setattr(session, "save_query_log", lambda *args: 1)

    def run(question: str, script: list[list[Any]]):
        monkeypatch.setattr(pipeline, "build_chat_model", lambda settings: ScriptedChatModel(script))
        settings = Settings(_env_file=None, as_of_date=AS_OF)
        return AskPipeline(settings, StubRetriever()).ask(question, "test")  # type: ignore[arg-type]

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


@pytest.fixture
def analyze(monkeypatch: pytest.MonkeyPatch):
    """Runs analysis mode through the real Agent loop, recording what would be stored."""
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

    def save_message(conn: object, session_id: str, role: str, content: str, *rest: Any) -> None:
        saved[role] = content

    monkeypatch.setattr(pipeline, "app_connection", no_connection)
    monkeypatch.setattr(tools, "execute_sql", fake_execute)
    monkeypatch.setattr(session, "ensure_conversation", lambda *args: None)
    monkeypatch.setattr(session, "load_history", lambda *args: [])
    monkeypatch.setattr(session, "save_message", save_message)
    monkeypatch.setattr(session, "save_query_log", save_log)

    def run(script: list[list[Any]], mode: str = "analysis"):
        monkeypatch.setattr(pipeline, "build_chat_model", lambda settings: ScriptedChatModel(script))
        settings = Settings(_env_file=None, as_of_date=AS_OF)
        answer = AskPipeline(settings, StubRetriever()).ask(  # type: ignore[arg-type]
            "分析一下最近30天的销量", "test",
            lambda event, data: saved["events"].append((event, data)), mode)  # type: ignore[arg-type]
        return answer, saved

    return run


def test_analysis_shows_only_the_cited_queries(analyze) -> None:
    answer, saved = analyze([
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
    assert "[查询" not in saved["assistant"]


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


# Re-asking

def test_rewind_runs_before_history_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def no_connection(settings: Settings):
        yield None

    calls: list[Any] = []

    def load_history(conn: object, session_id: str) -> list[tuple[str, str]]:
        calls.append("load")
        return []

    monkeypatch.setattr(pipeline, "app_connection", no_connection)
    monkeypatch.setattr(pipeline, "build_chat_model", lambda settings: None)
    monkeypatch.setattr(session, "ensure_conversation", lambda *args: None)
    monkeypatch.setattr(session, "rewind_history", lambda conn, sid, after: calls.append((sid, after)))
    monkeypatch.setattr(session, "load_history", load_history)
    monkeypatch.setattr(session, "save_message", lambda *args: 42)
    monkeypatch.setattr(session, "save_query_log", lambda *args: 1)

    settings = Settings(_env_file=None, as_of_date=AS_OF)
    answer = AskPipeline(settings, StubRetriever()).ask(  # type: ignore[arg-type]
        "上个月 GMV", "test", after_message_id=0)
    assert calls == [("test", 0), "load"]
    assert answer.message_id == 42


def test_a_new_question_does_not_rewind(ask, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any) -> None:
        raise AssertionError("history must stay")

    monkeypatch.setattr(session, "rewind_history", fail)
    answer = ask("上上个月哪个商品卖的最差", [[call("run_sql", sql=AUGUST_SQL)],
                                       [TextBlock(text="销量最差的是母婴热销款145号，仅63件。")]])
    assert answer.status == "ok"


def test_rewind_only_touches_later_messages_of_that_conversation() -> None:
    class Recorder:
        def execute(self, statement: Any, params: dict[str, Any]) -> None:
            self.sql, self.params = str(statement), params

    conn = Recorder()
    session.rewind_history(conn, "s1", 7)  # type: ignore[arg-type]
    assert "session_id = :session_id AND id > :after_id" in conn.sql
    assert conn.params == {"session_id": "s1", "after_id": 7}


def test_ask_request_rejects_a_negative_message_id() -> None:
    with pytest.raises(ValidationError):
        AskRequest(question="问题", after_message_id=-1)


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
    assert tools.UNKNOWN_COLUMN_HINT in tools._make_run_sql(context)(BAD_SQL)


def test_sql_failure_is_reported_once(monkeypatch: pytest.MonkeyPatch) -> None:
    context = failing_sql(monkeypatch)
    events: list[dict[str, Any]] = []
    context.emit = lambda event, data: events.append(data)
    reply = tools._make_run_sql(context)(BAD_SQL)
    assert reply.count("SQL 执行失败") == 1
    assert events[-1]["message"].count("SQL 执行失败") == 1


# Logging

def test_sql_failure_logs_the_database_error_and_the_statement(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    context = failing_sql(monkeypatch)
    with caplog.at_level(logging.INFO, logger="app"):
        tools._make_run_sql(context)(BAD_SQL)
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
        tools._make_run_sql(context)("DELETE FROM safe.orders")
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
