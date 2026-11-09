"""Whole tables go into the prompt: all of them within the budget, else the ones needed."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from app.agent import pipeline, tools
from app.agent.pipeline import SYSTEM_PROMPT, AskPipeline, _history_tables
from app.api.schemas import HistoryMessage, HistoryQuery
from app.config import Settings
from app.nl2sql.time_parser import parse_time_range
from app.semantic import repository
from app.semantic.models import (
    CaseHit,
    Metric,
    MetricHit,
    RetrievalBundle,
    SchemaHit,
    SchemaItem,
    TableSemantic,
)
from app.semantic.schema_context import (
    HEADER,
    REST_HEADER,
    build_schema_context,
    connect,
    estimate_tokens,
    join_graph,
)

PROJECT_DIR = Path(__file__).resolve().parents[2]
SEED_DIR = PROJECT_DIR / "data" / "seed"
SCHEMA_FILE = PROJECT_DIR / "data" / "initdb" / "01_business_schema.sql"
PREFIX = "safe."
AS_OF = date(2026, 10, 31)


def _columns() -> dict[str, list[tuple[str, str]]]:
    """The safe views' columns, typed as in the base tables."""
    text = SCHEMA_FILE.read_text(encoding="utf-8")
    types: dict[tuple[str, str], str] = {}
    for table, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\)", text, re.S):
        for column, column_type in re.findall(r"^\s+(\w+)\s+([A-Z]+(?:\([\d, ]+\))?)", body, re.M):
            types[(table, column)] = column_type.lower()
    views = re.findall(r"VIEW safe\.(\w+) AS\s*SELECT (.*?) FROM", text, re.S)
    return {table: [(name, types[(table, name)])
                    for name in (part.strip() for part in body.replace("\n", " ").split(","))]
            for table, body in views}


COLUMNS = _columns()
ITEMS = [SchemaItem.model_validate(row)
         for row in json.loads((SEED_DIR / "schema_items.json").read_text("utf-8"))]
SUMMARIES = {row["table_name"].removeprefix(PREFIX): row["summary"]
             for row in json.loads((SEED_DIR / "semantic_tables.json").read_text("utf-8"))}


def _build(wanted: list[str], budget: int) -> Any:
    return build_schema_context(COLUMNS, ITEMS, SUMMARIES, wanted, budget, PREFIX)


def test_every_table_fits_in_the_default_budget() -> None:
    schema = _build([], Settings(_env_file=None).schema_token_budget)
    assert schema.complete and sorted(schema.tables) == sorted(COLUMNS)
    assert schema.text.startswith(HEADER)
    assert REST_HEADER not in schema.text
    # Every column with its type, including those retrieval leaves out such as join keys.
    for columns in COLUMNS.values():
        for column, column_type in columns:
            assert f"- {column}（{column_type}）" in schema.text
    assert "safe.orders：订单主表" in schema.text
    orders_id = next(line for line in schema.text.splitlines()
                     if line.startswith("- id（bigint）：订单编号"))
    assert "（关联 safe.order_items.order_id）" in orders_id
    # The status values are in the descriptions.
    assert "取值 cancelled、paid、shipped、completed、refunded" in schema.text
    # Small enough not to matter: about 2k tokens.
    assert schema.tokens < 4000


def test_estimate_is_generous() -> None:
    assert estimate_tokens("订单") == 3
    assert estimate_tokens("select") == 3
    assert estimate_tokens("") == 1


def test_join_graph_reads_the_hints() -> None:
    graph = join_graph(ITEMS, PREFIX)
    assert {"orders", "products", "refunds"} <= graph["order_items"]
    assert "ad_daily_stats" in graph["ad_campaigns"]
    # "与 safe.shipments.province 对应" is no join.
    assert "shipments" not in graph.get("customers", set())


def test_connect_adds_the_tables_between() -> None:
    graph = join_graph(ITEMS, PREFIX)
    assert connect(["orders", "categories"], graph) == [
        "orders", "categories", "products", "order_items"]
    assert connect(["refunds", "refunds", "orders"], graph) == ["refunds", "orders"]
    assert connect(["orders"], graph) == ["orders"]
    # Nothing joins them: the table still comes in, alone.
    assert connect(["orders", "nowhere"], graph) == ["orders", "nowhere"]


def test_over_the_budget_only_the_needed_tables_come_in() -> None:
    schema = _build(["safe.orders", "categories"], 1300)
    assert not schema.complete
    assert schema.tables == ["orders", "categories", "products", "order_items"]
    listed, rest = schema.text.split(REST_HEADER)
    assert "- paid_at（datetime）" in listed and "- refund_amount" not in listed
    assert "- safe.refunds：退款表" in rest and "- safe.orders" not in rest


def test_the_most_needed_tables_come_first_when_the_budget_is_tight() -> None:
    # refunds and orders take about 860 tokens, ad_daily_stats 300 more.
    schema = _build(["refunds", "orders", "ad_daily_stats"], 1000)
    assert schema.tables == ["refunds", "orders"]
    # Even a budget below one table keeps the first.
    assert _build(["refunds"], 10).tables == ["refunds"]


def test_unknown_wanted_tables_are_skipped() -> None:
    schema = _build(["shop.secrets", "orders"], 300)
    assert schema.tables == ["orders"]
    assert "secrets" not in schema.text


# Where the wanted tables come from

def test_schema_context_puts_retrieval_first_then_the_history(
        monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def build(columns: Any, items: Any, summaries: Any, wanted: Any, budget: Any,
              prefix: Any) -> str:
        captured.update(wanted=wanted, summaries=summaries, budget=budget, prefix=prefix)
        return "schema"

    monkeypatch.setattr(tools, "table_columns", lambda settings: COLUMNS)
    monkeypatch.setattr(repository, "fetch_schema_items", lambda conn, enabled_only: ITEMS)
    monkeypatch.setattr(tools, "build_schema_context", build)
    item = next(item for item in ITEMS if item.table_name == "safe.shipments")
    bundle = RetrievalBundle(
        tables=[TableSemantic(table_name="safe.orders", summary="订单")],
        schema_items=[SchemaHit(item=item)],
        metrics=[MetricHit(metric=Metric(name="GMV", definition="", formula="",
                                         related_tables="safe.orders,safe.order_items"))],
        cases=[CaseHit(id=1, question="", sql_text="SELECT 1 FROM safe.refunds r")],
    )
    settings = Settings(_env_file=None, schema_token_budget=1234)
    assert tools.schema_context(settings, None, bundle, ["ad_daily_stats"]) == "schema"  # type: ignore[arg-type]
    assert captured["wanted"] == ["shipments", "orders", "order_items", "refunds",
                                  "ad_daily_stats"]
    assert captured["summaries"] == {"orders": "订单"}
    assert captured["budget"] == 1234 and captured["prefix"] == "safe."


def test_without_columns_there_is_no_schema_context() -> None:
    settings = Settings(_env_file=None)
    assert tools.schema_context(settings, None, RetrievalBundle(), []) is None  # type: ignore[arg-type]


def _reply(sql: str) -> HistoryMessage:
    return HistoryMessage(role="assistant", content="好", status="ok",
                          queries=[HistoryQuery(purpose="", sql=sql)])


def test_history_tables_come_from_the_last_three_replies_newest_first() -> None:
    history = [
        HistoryMessage(role="user", content="一"), _reply("SELECT * FROM safe.shipments"),
        HistoryMessage(role="user", content="二"), _reply("SELECT * FROM safe.refunds"),
        HistoryMessage(role="user", content="三"),
        _reply("SELECT * FROM safe.orders o JOIN safe.order_items oi ON oi.order_id = o.id"),
        HistoryMessage(role="user", content="四"), _reply("SELECT * FROM SAFE.Orders"),
    ]
    assert _history_tables(history, "safe") == ["orders", "order_items", "refunds"]


# The prompt

def _prompt(schema: Any) -> str:
    settings = Settings(_env_file=None, as_of_date=AS_OF)
    item = next(item for item in ITEMS if item.column_name == "paid_at")
    bundle = RetrievalBundle(
        tables=[TableSemantic(table_name="safe.orders", summary="订单主表")],
        schema_items=[SchemaHit(item=item)],
        metrics=[MetricHit(metric=Metric(name="GMV", definition="成交额", formula="SUM(x)"))],
    )
    return AskPipeline(settings, None)._system_prompt(  # type: ignore[arg-type]
        bundle, parse_time_range("上个月", AS_OF), "query", schema=schema)


def test_the_prompt_lists_whole_tables_before_what_changes() -> None:
    schema = _build([], 32000)
    prompt = _prompt(schema)
    assert prompt.startswith(SYSTEM_PROMPT + "\n数据库：MySQL，SQL 按 MySQL 的语法写。业务库名：safe。\n"
                             + "\n" + schema.text)
    assert prompt.index(HEADER) < prompt.index("本次统计截止日") < prompt.index("【指标口径】\n")
    # The retrieved columns and summaries would only repeat the tables.
    assert "【字段】（" not in prompt and "【表摘要】" not in prompt


def test_without_whole_tables_the_prompt_keeps_the_retrieved_columns() -> None:
    prompt = _prompt(None)
    assert HEADER not in prompt
    assert "【字段】（只列出一部分，没见过的字段先调用 describe_table）" in prompt
    assert "【表摘要】" in prompt


def test_rule_eleven_says_listed_tables_are_complete() -> None:
    assert "【表结构】里列出的表包含全部字段，没列出的字段就不存在" in SYSTEM_PROMPT
    assert "只列出一部分" not in SYSTEM_PROMPT


def test_the_pipeline_falls_back_when_the_schema_cannot_be_read(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from sqlalchemy.exc import OperationalError

    def fail(*args: Any) -> None:
        raise OperationalError("SELECT", {}, Exception("down"))

    monkeypatch.setattr(pipeline, "schema_context", fail)
    monkeypatch.setattr(pipeline, "agentic_context", fail)
    settings = Settings(_env_file=None)
    assert AskPipeline(settings, None)._schema(None, RetrievalBundle(), [], "t") is None  # type: ignore[arg-type]
    assert "读取表结构失败" in caplog.text


# Nothing about this database is written into the rules

BUSINESS_TERMS = ("orders", "refunds", "categories", "paid_at", "completed_at", "cancelled",
                  "shop", "safe")


def test_the_rules_name_no_table_column_or_schema() -> None:
    from app.agent.pipeline import ANALYSIS_PROMPT
    for prompt in (SYSTEM_PROMPT, ANALYSIS_PROMPT):
        assert [term for term in BUSINESS_TERMS if term in prompt] == []
    assert "按【指标口径】的公式和表结构里的字段说明执行" in SYSTEM_PROMPT
    # A definition the user names is no attempt to get around the rules.
    assert "用户明确指定了口径时" in SYSTEM_PROMPT and "这不算绕过规则" in SYSTEM_PROMPT


def test_the_planner_reads_the_tables_from_the_knowledge() -> None:
    from app.agent import planner
    assert [term for term in BUSINESS_TERMS if term in planner.PLAN_PROMPT] == []
    assert "【表摘要】" in planner.PLAN_PROMPT


def test_the_database_note_follows_the_settings() -> None:
    settings = Settings(_env_file=None, mysql_business_db="public", sql_dialect="postgres")
    note = AskPipeline(settings, None)._database_note()  # type: ignore[arg-type]
    assert note == "数据库：PostgreSQL，SQL 按 PostgreSQL 的语法写。业务库名：public。\n"
