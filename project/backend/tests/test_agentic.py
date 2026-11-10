"""The model searches the tables itself: catalog, recent tables, search tool, visibility."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from agentscope.message import TextBlock
from app.agent import pipeline, session, tools
from app.agent.pipeline import (
    AGENTIC_RULE_COLUMNS,
    AGENTIC_RULE_TABLES,
    ANALYSIS_PROMPT,
    RULE_COLUMNS,
    RULE_TABLES,
    SYSTEM_PROMPT,
    AskPipeline,
    agentic_rules,
)
from app.agent.scope_model import ScriptedChatModel
from app.api.schemas import HistoryMessage, HistoryQuery
from app.config import Settings
from app.nl2sql.executor import ExecutionResult
from app.nl2sql.time_parser import parse_time_range
from app.safety.validator import SQLPolicy, validate_and_limit
from app.semantic import repository
from app.semantic.models import Metric, MetricHit, RetrievalBundle, SchemaItem, TableSemantic
from app.semantic.schema_context import CATALOG_HEADER, PREVIOUS_HEADER, build_agentic_context
from app.semantic.table_search import (
    NOTHING_FOUND,
    SEARCH_HEADER,
    TableChoice,
    TableSearch,
    TableSearcher,
    render_search,
)
from business_schema import COLUMNS as VIEW_COLUMNS
from test_pipeline import AS_OF, call

SEED = Path(__file__).resolve().parents[2] / "data" / "seed"
ITEMS = [SchemaItem.model_validate(row)
         for row in json.loads((SEED / "schema_items.json").read_text("utf-8"))]
TABLES = [TableSemantic.model_validate(row)
          for row in json.loads((SEED / "semantic_tables.json").read_text("utf-8"))]
SUMMARIES = {row.table_name.removeprefix("safe."): row.summary for row in TABLES}
COLUMNS = {table: [(column, "text") for column in names] for table, names in VIEW_COLUMNS.items()}
PREFIX = "safe."


# The catalog and the recent tables

def test_the_catalog_lists_every_table_and_the_recent_ones_in_full() -> None:
    context = build_agentic_context(COLUMNS, ITEMS, SUMMARIES, ["refunds", "safe.orders"],
                                    32000, PREFIX)
    assert context.agentic and context.tables == ["refunds", "orders"]
    catalog, previous = context.text.split(PREVIOUS_HEADER)
    assert catalog.startswith(CATALOG_HEADER)
    assert "- safe.channels：订单来源渠道字典表" in catalog
    assert "- channel_id（text）" not in catalog
    assert previous.index("safe.refunds：") < previous.index("safe.orders：")
    assert "- refund_amount（text）：退款金额" in previous


def test_without_recent_tables_only_the_catalog_goes_in() -> None:
    context = build_agentic_context(COLUMNS, ITEMS, SUMMARIES, [], 32000, PREFIX)
    assert context.tables == [] and PREVIOUS_HEADER not in context.text
    assert context.text.count("\n- safe.") == len(COLUMNS)


def test_a_tight_budget_shortens_the_catalog_then_drops_it() -> None:
    names_only = build_agentic_context(COLUMNS, ITEMS, SUMMARIES, [], 200, PREFIX)
    assert "- safe.orders\n" in names_only.text and "订单主表" not in names_only.text
    assert build_agentic_context(COLUMNS, ITEMS, SUMMARIES, ["orders"], 10, PREFIX).text == ""


def test_recent_tables_stop_at_the_budget() -> None:
    context = build_agentic_context(COLUMNS, ITEMS, SUMMARIES, ["orders", "refunds"], 900,
                                    PREFIX)
    assert context.tables == ["orders"]


# The rules

def test_agentic_rules_replace_the_table_and_column_rules() -> None:
    for prompt in (SYSTEM_PROMPT, ANALYSIS_PROMPT):
        assert RULE_TABLES in prompt and RULE_COLUMNS in prompt
        swapped = agentic_rules(prompt)
        assert AGENTIC_RULE_TABLES in swapped and AGENTIC_RULE_COLUMNS in swapped
        assert RULE_TABLES not in swapped and RULE_COLUMNS not in swapped
    assert "先调用 search_semantic 检索要用的表" in AGENTIC_RULE_COLUMNS
    assert "不需要查数时不用检索" in AGENTIC_RULE_COLUMNS


def _prompt(schema: Any, mode: str = "query") -> str:
    settings = Settings(_env_file=None, as_of_date=AS_OF)
    item = next(item for item in ITEMS if item.column_name == "paid_at")
    from app.semantic.models import SchemaHit
    bundle = RetrievalBundle(tables=TABLES, schema_items=[SchemaHit(item=item)],
                             metrics=[MetricHit(metric=Metric(name="GMV", definition="成交额",
                                                              formula="SUM(x)"))])
    return AskPipeline(settings, None)._system_prompt(  # type: ignore[arg-type]
        bundle, parse_time_range("上个月", AS_OF), mode, schema=schema)  # type: ignore[arg-type]


def test_the_agentic_prompt_has_the_catalog_and_its_rules() -> None:
    schema = build_agentic_context(COLUMNS, ITEMS, SUMMARIES, [], 32000, PREFIX)
    for mode in ("query", "analysis"):
        prompt = _prompt(schema, mode)
        assert AGENTIC_RULE_COLUMNS in prompt and CATALOG_HEADER in prompt
        # The catalog is the same for every question, so it comes before the time range.
        assert prompt.index(CATALOG_HEADER) < prompt.index("本次统计截止日")
        # The retrieved columns and summaries would repeat the catalog.
        assert "【字段】（" not in prompt and "【表摘要】" not in prompt
        assert "【指标口径】\n- GMV" in prompt


def test_the_mode_picks_the_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(pipeline, "agentic_context", lambda *args: calls.append("agentic"))
    monkeypatch.setattr(pipeline, "schema_context", lambda *args: calls.append("full"))
    for mode in ("agentic", "full"):
        settings = Settings(_env_file=None, schema_mode=mode)
        AskPipeline(settings, None)._schema(None, RetrievalBundle(), [], "t")  # type: ignore[arg-type]
    assert calls == ["agentic", "full"]


# What search_semantic returns

def _search(*choices: TableChoice, **extra: Any) -> TableSearch:
    return TableSearch(query="q", tables=list(choices), candidates=[], metrics=extra.get(
        "metrics", []), cases=[], entities=[])


def test_the_search_result_writes_out_every_column_and_the_reasons() -> None:
    search = _search(TableChoice("orders", ["重排选中"], 0.9),
                     TableChoice("channels", ["外键指向"]),
                     metrics=[MetricHit(metric=Metric(name="订单数", definition="d", formula="f"))])
    text, shown = render_search(search, COLUMNS, ITEMS, SUMMARIES, 32000, PREFIX)
    assert shown == ["orders", "channels"]
    assert text.startswith(SEARCH_HEADER)
    assert "safe.orders：订单主表，一行一个订单" in text and "（重排选中）" in text
    assert "- channel_id（text）：订单来源渠道编号" in text
    assert "safe.channels：" in text and "（外键指向）" in text
    assert "【指标口径】\n- 订单数" in text


def test_over_the_budget_the_lowest_scored_chosen_tables_go_first() -> None:
    search = _search(TableChoice("orders", ["重排选中"], 0.9),
                     TableChoice("refunds", ["重排选中"], 0.2),
                     TableChoice("order_items", ["关联补全"]))
    # orders and order_items take about 710 tokens, refunds 360 more.
    text, shown = render_search(search, COLUMNS, ITEMS, SUMMARIES, 900, PREFIX)
    # The joining table stays; the weaker chosen one is left out and named.
    assert shown == ["orders", "order_items"]
    assert "还有这些表因为长度限制没有放进来：safe.refunds" in text


def test_a_search_that_finds_nothing_says_so() -> None:
    text, shown = render_search(_search(), COLUMNS, ITEMS, SUMMARIES, 32000, PREFIX)
    assert shown == [] and text == NOTHING_FOUND


# Which tables a query may read

def test_the_validator_reports_the_tables_a_query_reads() -> None:
    policy = SQLPolicy(schema="safe", dialect="mysql")
    safe = validate_and_limit("WITH t AS (SELECT id FROM safe.Orders) SELECT * FROM t "
                              "JOIN safe.refunds r ON r.order_id = t.id", 500, policy)
    assert set(safe.tables) == {"orders", "refunds"}


def _context(**fields: Any) -> tools.AgentRunContext:
    return tools.AgentRunContext(settings=Settings(_env_file=None), retriever=None,  # type: ignore[arg-type]
                                 **fields)


def test_only_seen_tables_pass_when_the_model_searches() -> None:
    context = _context(require_visible=True, visible_tables={"orders"})
    assert tools.unseen_tables(context, ("orders", "refunds")) == ["refunds"]
    # With every table in the prompt there is nothing to check.
    assert tools.unseen_tables(_context(), ("refunds",)) == []


def test_run_sql_turns_back_a_table_not_searched(monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[str] = []
    monkeypatch.setattr(tools, "execute_sql", lambda settings, safe: (
        executed.append(safe.sql), ExecutionResult(columns=["n"], rows=[[1]], sql=safe.sql))[1])
    context = _context(require_visible=True, visible_tables={"orders"})
    run_sql = tools._make_run_sql(context)
    reply = asyncio.run(run_sql("SELECT COUNT(*) FROM safe.refunds"))
    assert reply == tools.UNSEEN_TABLE_MESSAGE.format(tables="refunds")
    assert executed == [] and context.failed_sql == "SELECT COUNT(*) FROM safe.refunds"
    context.visible_tables.add("refunds")
    assert '"rows"' in asyncio.run(run_sql("SELECT COUNT(*) FROM safe.refunds"))
    assert len(executed) == 1


def test_turned_back_queries_count_towards_the_failure_limit() -> None:
    context = _context(require_visible=True)
    run_sql = tools._make_run_sql(context)
    replies = [asyncio.run(run_sql("SELECT 1 FROM safe.refunds")) for _ in range(3)]
    assert "SQL 已经连续失败 3 次" in replies[2]


def test_describe_table_makes_a_table_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "describe_tables", lambda settings, names: "字段")
    monkeypatch.setattr(tools, "table_columns", lambda settings: COLUMNS)
    context = _context(require_visible=True)
    asyncio.run(tools._make_describe(context)(["safe.refunds", "nothing"]))
    assert context.visible_tables == {"refunds"}


def test_the_toolkit_has_the_table_search_when_there_is_a_searcher() -> None:
    plain = tools.build_toolkit(_context())
    searching = tools.build_toolkit(_context(searcher=object()))
    assert "检索要用的表和它们的全部字段" in str(asyncio.run(searching.get_tool_schemas()))
    assert "检索要用的表和它们的全部字段" not in str(asyncio.run(plain.get_tool_schemas()))


# The whole question

@contextmanager
def _no_connection(settings: Settings):
    yield None


class _Retriever:
    def retrieve_with_connection(self, question: str, conn: Any,
                                 trace: Any = None) -> RetrievalBundle:
        return RetrievalBundle(tables=TABLES)


@pytest.fixture
def agentic(monkeypatch: pytest.MonkeyPatch):
    searches: list[str] = []
    executed: list[str] = []

    def search(self: TableSearcher, query: str, conn: Any, **kwargs: Any) -> TableSearch:
        searches.append(query)
        return _search(TableChoice("products", ["重排选中"], 0.9),
                       TableChoice("order_items", ["外键指向"]))

    def execute(settings: Settings, safe: Any) -> ExecutionResult:
        executed.append(safe.sql)
        return ExecutionResult(columns=["name", "qty"], rows=[["童装热销款208号", 176]],
                               sql=safe.sql)

    monkeypatch.setattr(pipeline, "app_connection", _no_connection)
    monkeypatch.setattr(tools, "app_connection", _no_connection)
    monkeypatch.setattr(tools, "table_columns", lambda settings: COLUMNS)
    monkeypatch.setattr(tools, "execute_sql", execute)
    monkeypatch.setattr(repository, "fetch_schema_items", lambda conn, enabled_only=True: ITEMS)
    monkeypatch.setattr(repository, "fetch_tables", lambda conn: TABLES)
    monkeypatch.setattr(TableSearcher, "search", search)
    monkeypatch.setattr(session, "save_query_log", lambda *args, **kwargs: 1)
    monkeypatch.setattr(session, "save_call_traces", lambda *args: None)
    monkeypatch.setattr(session, "save_query_executions", lambda *args: None)

    def run(script: list[list[Any]], schema_mode: str = "agentic",
            history: list[HistoryMessage] | None = None) -> tuple[Any, list[dict]]:
        models = iter([ScriptedChatModel(script), ScriptedChatModel([[]])])
        monkeypatch.setattr(pipeline, "build_chat_model", lambda settings: next(models))
        events: list[dict] = []
        settings = Settings(_env_file=None, as_of_date=AS_OF, schema_mode=schema_mode)
        answer = AskPipeline(settings, _Retriever()).ask(  # type: ignore[arg-type]
            "上个月销量最高的商品是哪个？", "test", lambda event, data: events.append(data),
            "query", history or [])
        return answer, events

    run.searches = searches  # type: ignore[attr-defined]
    run.executed = executed  # type: ignore[attr-defined]
    return run


SALES_SQL = ("SELECT p.name, SUM(oi.quantity) AS qty FROM safe.products p "
             "JOIN safe.order_items oi ON oi.product_id = p.id GROUP BY p.name "
             "ORDER BY qty DESC LIMIT 1")


def test_the_model_searches_then_queries(agentic) -> None:
    answer, events = agentic([
        [call("run_sql", sql=SALES_SQL)],
        [call("search_semantic", question="上个月各商品的销量")],
        [call("run_sql", sql=SALES_SQL)],
        [TextBlock(text="上个月销量最高的是童装热销款208号，176件。")],
    ])
    assert answer.status == "ok"
    retrying = [event["message"] for event in events if event.get("step") == "retrying"]
    assert retrying == ["SQL 用了没检索过的表：products、order_items"]
    assert agentic.searches == ["上个月各商品的销量"]
    assert len(agentic.executed) == 1
    assert answer.retries == 2


def test_the_tables_of_the_last_reply_need_no_search(agentic) -> None:
    history = [HistoryMessage(role="user", content="上个月卖得最多的是什么？"),
               HistoryMessage(role="assistant", content="童装热销款208号。", status="ok",
                              queries=[HistoryQuery(purpose="销量", sql=SALES_SQL)])]
    answer, _ = agentic([[call("run_sql", sql=SALES_SQL)],
                         [TextBlock(text="上个月销量最高的是童装热销款208号，176件。")]],
                        history=history)
    assert answer.status == "ok" and agentic.searches == []


def test_with_every_table_in_the_prompt_nothing_is_checked(agentic) -> None:
    answer, _ = agentic([[call("run_sql", sql=SALES_SQL)],
                         [TextBlock(text="上个月销量最高的是童装热销款208号，176件。")]],
                        schema_mode="full")
    assert answer.status == "ok" and len(agentic.executed) == 1


def test_an_answer_records_its_sql_runs_and_the_log_write_time(
        agentic, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.trace import text_hash
    saved: list[Any] = []
    monkeypatch.setattr(session, "save_call_traces",
                        lambda conn, log_id, records: saved.extend(records))
    answer, _ = agentic([
        [call("search_semantic", question="上个月各商品的销量")],
        [call("run_sql", sql=SALES_SQL)],
        [TextBlock(text="上个月销量最高的是童装热销款208号，176件。")],
    ])
    kinds = [(record.kind, record.name) for record in saved]
    assert ("tool", "search_semantic") in kinds and ("tool", "run_sql") in kinds
    sql = [record for record in saved if record.kind == "sql"]
    assert len(sql) == 1 and sql[0].name == "execute_sql" and sql[0].ok
    assert sql[0].input_hash == text_hash(agentic.executed[0])
    assert answer.usage.tool_calls == 2
    assert answer.usage.log_write_ms >= 0
