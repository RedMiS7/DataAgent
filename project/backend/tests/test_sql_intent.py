"""Checking a SQL against how its question was understood (plan 2.3)."""

from __future__ import annotations

import itertools
import json
from contextlib import contextmanager
from datetime import date
from typing import Any

import pytest
from agentscope.message import TextBlock, ToolCallBlock
from app.agent import pipeline, session, tools
from app.agent.intent import Catalog, intent_from_dict
from app.agent.pipeline import AskPipeline
from app.agent.scope_model import ScriptedChatModel
from app.agent.sql_intent import (
    BLOCK_PREFIX,
    WARNING_PREFIX,
    OneToMany,
    SQLChecks,
    build_checks,
    check_sql,
    one_to_many,
)
from app.config import PROJECT_DIR, Settings
from app.evaluation.runner import Activity
from app.nl2sql.executor import ExecutionResult
from app.nl2sql.time_parser import parse_time_range
from app.semantic.models import RetrievalBundle
from app.semantic.repository import load_seed

AS_OF = date(2026, 10, 31)
SEED = load_seed(PROJECT_DIR / "data" / "seed")
CALL_IDS = itertools.count(1)

# What i02 ran on 2026-11-10 (run 8): the refund and the GMV, but no ratio.
I02_SQL = (
    "SELECT (SELECT SUM(r.refund_amount) FROM safe.refunds AS r WHERE r.status = 'completed' "
    "AND r.completed_at >= '2026-09-01' AND r.completed_at < '2026-10-01') AS refund_amount, "
    "(SELECT SUM(oi.quantity * oi.unit_price - oi.discount_amount) FROM safe.orders AS o "
    "JOIN safe.order_items AS oi ON oi.order_id = o.id WHERE o.status <> 'cancelled' "
    "AND o.paid_at >= '2026-09-01' AND o.paid_at < '2026-10-01') AS gmv")
I02_FIXED = (
    "SELECT (SELECT SUM(r.refund_amount) FROM safe.refunds AS r WHERE r.status = 'completed' "
    "AND r.completed_at >= '2026-09-01' AND r.completed_at < '2026-10-01') / "
    "(SELECT SUM(oi.quantity * oi.unit_price - oi.discount_amount) FROM safe.orders AS o "
    "JOIN safe.order_items AS oi ON oi.order_id = o.id WHERE o.status <> 'cancelled' "
    "AND o.paid_at >= '2026-09-01' AND o.paid_at < '2026-10-01') * 100 AS 退货率")
# f05 on 2026-11-09: "which product sold worst" answered with ten products.
F05_SQL = ("SELECT p.name, SUM(oi.quantity) AS qty FROM safe.orders o "
           "JOIN safe.order_items oi ON oi.order_id = o.id JOIN safe.products p ON p.id = oi.product_id "
           "WHERE o.status <> 'cancelled' AND o.paid_at >= '2026-08-01' AND o.paid_at < '2026-09-01' "
           "GROUP BY p.id, p.name ORDER BY qty ASC LIMIT 10")


def checks(question: str = "上个月", mode: str = "query", **fields: Any) -> SQLChecks:
    intent = intent_from_dict({"route": "query", "time_range": {"text": question}, **fields})
    return build_checks(intent, parse_time_range(question, AS_OF), SEED.metrics,
                        SEED.schema_items, "safe", mode)


def kinds(sql: str, wanted: SQLChecks) -> list[str]:
    return [item.kind for item in check_sql(sql, wanted, "safe", "mysql")]


GMV_SEPTEMBER = ("SELECT SUM(oi.quantity * oi.unit_price) AS gmv FROM safe.orders o "
                 "JOIN safe.order_items oi ON oi.order_id = o.id "
                 "WHERE o.paid_at >= '2026-09-01' AND o.paid_at < '2026-10-01'")


# Regressions

def test_i02_a_rate_without_a_ratio_is_turned_back() -> None:
    wanted = checks(metrics=[{"name": "退货率"}], result_shape={"type": "single"})
    assert kinds(I02_SQL, wanted) == ["ratio"]
    assert kinds(I02_FIXED, wanted) == []


def test_f05_the_worst_one_is_not_ten_rows() -> None:
    wanted = checks("上上个月", metrics=[{"name": "商品销量"}],
                    result_shape={"type": "top_n", "n": 1, "order": "asc"})
    assert kinds(F05_SQL, wanted) == ["shape"]
    assert kinds(F05_SQL.replace("LIMIT 10", "LIMIT 1"), wanted) == []


# Each check, with a SQL that passes and one that does not

def test_the_period_must_match() -> None:
    wanted = checks(metrics=[{"name": "GMV"}])
    assert kinds(GMV_SEPTEMBER, wanted) == []
    assert kinds(GMV_SEPTEMBER.replace("2026-09-01", "2026-10-01")
                 .replace("< '2026-10-01'", "< '2026-11-01'"), wanted) == ["time"]
    # BETWEEN ends on the last day.
    assert kinds(GMV_SEPTEMBER.replace(
        "o.paid_at >= '2026-09-01' AND o.paid_at < '2026-10-01'",
        "o.paid_at BETWEEN '2026-09-01' AND '2026-09-30'"), wanted) == []


def test_a_named_period_needs_a_date_and_a_default_one_does_not() -> None:
    no_dates = ("SELECT SUM(oi.quantity) FROM safe.orders o "
                "JOIN safe.order_items oi ON oi.order_id = o.id")
    assert kinds(no_dates, checks(metrics=[{"name": "商品销量"}])) == ["time"]
    assert kinds(no_dates, checks("", metrics=[{"name": "商品销量"}])) == []
    assert kinds(no_dates, checks(time_range={"needed": False})) == []


def test_a_comparison_needs_the_baseline_dates_too() -> None:
    wanted = build_checks(
        intent_from_dict({"route": "query", "metrics": [{"name": "GMV"}],
                          "time_range": {"text": "上个月"}}),
        parse_time_range("上个月比上上个月高多少", AS_OF), SEED.metrics, SEED.schema_items,
        "safe", "query")
    assert kinds(GMV_SEPTEMBER, wanted) == ["time"]
    both = GMV_SEPTEMBER + " UNION ALL " + GMV_SEPTEMBER.replace("09-01", "08-01").replace(
        "10-01", "09-01")
    assert "time" not in kinds(both, wanted)


def test_a_metric_needs_the_tables_of_its_definition() -> None:
    wanted = checks(metrics=[{"name": "GMV"}])
    only_orders = ("SELECT SUM(o.total_amount) FROM safe.orders o "
                   "WHERE o.paid_at >= '2026-09-01' AND o.paid_at < '2026-10-01'")
    assert kinds(only_orders, wanted) == ["metric_tables"]
    message = check_sql(only_orders, wanted, "safe", "mysql")[0].message
    assert "order_items" in message
    # A metric outside the semantic layer has no tables to check.
    assert kinds(only_orders, checks(metrics=[{"custom": "订单金额合计"}])) == []


def test_a_ratio_shape_needs_a_division() -> None:
    wanted = checks(metrics=[{"custom": "取消订单占比"}], result_shape={"type": "ratio"})
    count = ("SELECT COUNT(*) FROM safe.orders o WHERE o.status = 'cancelled' "
             "AND o.paid_at >= '2026-09-01' AND o.paid_at < '2026-10-01'")
    assert kinds(count, wanted) == ["ratio"]
    assert kinds(count.replace("COUNT(*)", "SUM(o.status = 'cancelled') / COUNT(*)"), wanted) == []


def test_a_dimension_must_be_grouped_by() -> None:
    wanted = checks(metrics=[{"name": "GMV"}],
                    dimensions=[{"term": "渠道", "field": "channels.name"}])
    grouped = ("SELECT c.name, SUM(oi.quantity * oi.unit_price) FROM safe.orders o "
               "JOIN safe.order_items oi ON oi.order_id = o.id "
               "JOIN safe.channels c ON c.id = o.channel_id "
               "WHERE o.paid_at >= '2026-09-01' AND o.paid_at < '2026-10-01' GROUP BY c.name")
    assert kinds(grouped, wanted) == []
    # GROUP BY a position or an alias, or by the channel id, counts too.
    assert kinds(grouped.replace("GROUP BY c.name", "GROUP BY 1"), wanted) == []
    assert kinds(grouped.replace("SELECT c.name,", "SELECT c.name AS 渠道,")
                 .replace("GROUP BY c.name", "GROUP BY 渠道"), wanted) == []
    assert kinds(grouped.replace("GROUP BY c.name", "GROUP BY c.id, c.name"), wanted) == []
    assert kinds(GMV_SEPTEMBER, wanted) == ["group"]
    by_province = grouped.replace("GROUP BY c.name", "GROUP BY o.province")
    assert kinds(by_province, wanted) == ["group"]


def test_a_dimension_without_a_column_is_not_checked() -> None:
    wanted = checks(metrics=[{"name": "GMV"}], dimensions=[{"term": "渠道"}])
    assert kinds(GMV_SEPTEMBER, wanted) == []


def test_a_filter_value_must_be_in_the_conditions() -> None:
    wanted = checks(metrics=[{"name": "GMV"}],
                    filters=[{"term": "渠道", "field": "channels.name", "value": "直播带货"}])
    filtered = GMV_SEPTEMBER.replace(
        "FROM safe.orders o", "FROM safe.orders o JOIN safe.channels c ON c.id = o.channel_id"
    ) + " AND c.name = '直播带货'"
    assert kinds(filtered, wanted) == []
    assert kinds(filtered.replace("= '直播带货'", "LIKE '%直播带货%'"), wanted) == []
    assert kinds(filtered.replace("= '直播带货'", "IN ('直播带货', '短视频')"), wanted) == []
    assert kinds(GMV_SEPTEMBER, wanted) == ["filter"]


def test_top_n_needs_an_order_and_a_limit_no_larger_than_n() -> None:
    wanted = checks(metrics=[{"name": "商品销量"}],
                    result_shape={"type": "top_n", "n": 10, "order": "desc"})
    top = F05_SQL.replace("2026-08-01", "2026-09-01").replace("< '2026-09-01'", "< '2026-10-01'")
    assert kinds(top, wanted) == []
    assert kinds(top.replace(" LIMIT 10", ""), wanted) == ["shape"]
    assert kinds(top.replace("ORDER BY qty ASC ", ""), wanted) == ["shape"]
    assert kinds(top.replace("LIMIT 10", "LIMIT 5"), wanted) == []


def test_a_single_value_is_not_grouped_and_a_trend_is() -> None:
    single = checks(metrics=[{"name": "GMV"}], result_shape={"type": "single"})
    grouped = GMV_SEPTEMBER.replace("SELECT SUM", "SELECT o.province, SUM") + " GROUP BY o.province"
    assert kinds(GMV_SEPTEMBER, single) == []
    assert kinds(grouped, single) == ["shape"]
    trend = checks(metrics=[{"name": "GMV"}], result_shape={"type": "trend", "grain": "day"})
    assert kinds(GMV_SEPTEMBER, trend) == ["shape"]
    assert kinds(GMV_SEPTEMBER.replace("SELECT SUM", "SELECT DATE(o.paid_at) AS d, SUM")
                 + " GROUP BY d", trend) == []


# Rows counted after a join repeated them

def test_the_join_hints_give_the_one_side() -> None:
    relations = one_to_many(SEED.schema_items, "safe")
    assert OneToMany("orders", "id", "order_items", "order_id") in relations
    assert OneToMany("categories", "id", "products", "category_id") in relations
    # "与 safe.shipments.province 对应" links no id, so it says nothing about repeats.
    assert not any("province" in (item.one_column, item.many_column) for item in relations)


@pytest.mark.parametrize("sql, wrong", [
    ("SELECT COUNT(*) FROM safe.orders o JOIN safe.order_items oi ON oi.order_id = o.id", True),
    ("SELECT COUNT(o.id) FROM safe.orders o JOIN safe.order_items oi ON oi.order_id = o.id", True),
    ("SELECT COUNT(DISTINCT o.id) FROM safe.orders o "
     "JOIN safe.order_items oi ON oi.order_id = o.id", False),
    # Counting the many side is right: each product is one row.
    ("SELECT COUNT(*) FROM safe.products p JOIN safe.categories c ON c.id = p.category_id", False),
    ("SELECT COUNT(oi.id) FROM safe.orders o JOIN safe.order_items oi ON oi.order_id = o.id", False),
    # No join, no repeat.
    ("SELECT COUNT(*) FROM safe.orders o", False),
    # The count is in a subquery that joins nothing.
    ("SELECT x.n FROM (SELECT COUNT(*) AS n FROM safe.orders) x "
     "JOIN safe.order_items oi ON 1 = 1", False),
])
def test_a_count_of_the_one_side_after_joining_the_many_side(sql: str, wrong: bool) -> None:
    only_joins = SQLChecks(relations=one_to_many(SEED.schema_items, "safe"))
    assert kinds(sql, only_joins) == (["fan_out"] if wrong else [])


def test_analysis_mode_checks_only_the_joins() -> None:
    wanted = checks(mode="analysis", metrics=[{"name": "GMV"}], result_shape={"type": "single"})
    assert wanted.only_fan_out
    assert kinds("SELECT o.province, SUM(1) FROM safe.orders o GROUP BY o.province", wanted) == []
    assert kinds("SELECT COUNT(*) FROM safe.orders o "
                 "JOIN safe.order_items oi ON oi.order_id = o.id", wanted) == ["fan_out"]


def test_without_an_intent_only_the_joins_are_checked() -> None:
    wanted = build_checks(None, parse_time_range("上个月", AS_OF), SEED.metrics,
                          SEED.schema_items, "safe", "query")
    assert wanted.relations and wanted.time_range is None and not wanted.metric_tables
    assert kinds(GMV_SEPTEMBER.replace("2026", "2025"), wanted) == []


def test_sql_that_cannot_be_parsed_is_left_to_the_database() -> None:
    assert kinds("SELECT FROM WHERE", checks(metrics=[{"name": "GMV"}])) == []


# In the pipeline: turned back once, then let through with a warning

def call(sql: str) -> ToolCallBlock:
    return ToolCallBlock(type="tool_call", id=f"sql-intent-{next(CALL_IDS)}", name="run_sql",
                         input=json.dumps({"sql": sql}, ensure_ascii=False))


class StubRetriever:
    def retrieve_with_connection(self, question: str, conn: object,
                                 trace: Any = None) -> RetrievalBundle:
        return RetrievalBundle()


@pytest.fixture
def ask(monkeypatch: pytest.MonkeyPatch):
    @contextmanager
    def no_connection(settings: Settings):
        yield None

    executed: list[str] = []
    saved: dict[str, Any] = {}

    def fake_execute(settings: Settings, safe: Any) -> ExecutionResult:
        executed.append(safe.sql)
        return ExecutionResult(columns=["退货率"], rows=[[3.93]], sql=safe.sql)

    monkeypatch.setattr(pipeline, "app_connection", no_connection)
    monkeypatch.setattr(pipeline, "load_catalog", lambda settings, conn: Catalog(
        metrics=SEED.metrics, schema_items=SEED.schema_items))
    monkeypatch.setattr(tools, "execute_sql", fake_execute)
    monkeypatch.setattr(session, "save_query_log", lambda *args, **kwargs: 1)
    monkeypatch.setattr(session, "save_call_traces", lambda *args: None)
    monkeypatch.setattr(session, "save_query_executions",
                        lambda conn, log_id, records: saved.__setitem__("queries", records))
    intent = json.dumps({"route": "query", "reason": "数字", "metrics": [{"name": "退货率"}],
                         "time_range": {"text": "上个月"}}, ensure_ascii=False)

    def run(script: list[list[Any]]):
        monkeypatch.setattr(pipeline, "build_router_model",
                            lambda settings: ScriptedChatModel([[TextBlock(text=intent)]]))
        monkeypatch.setattr(pipeline, "build_chat_model",
                            lambda settings: ScriptedChatModel(script))
        answer = AskPipeline(Settings(_env_file=None, as_of_date=AS_OF),
                             StubRetriever()).ask(  # type: ignore[arg-type]
            "上个月的退货率是多少？", "test", None, "query", [])
        return answer, executed, saved

    return run


def test_a_mismatched_sql_is_turned_back_and_the_fix_runs(ask) -> None:
    answer, executed, saved = ask([[call(I02_SQL)], [call(I02_FIXED)],
                                   [TextBlock(text="上个月退货率 3.93%。")]])
    assert answer.status == "ok"
    # Only the fixed SQL reached the database.
    assert len(executed) == 1 and executed[0].startswith(I02_FIXED)
    assert answer.warnings == []
    blocked = saved["queries"][0]
    assert blocked.status == "blocked" and blocked.error.startswith(BLOCK_PREFIX)
    assert "除法" in blocked.error
    assert Activity.from_archive(
        [{"status": record.status, "error": record.error, "sql_hash": ""}
         for record in saved["queries"]], []).intent_blocks == 1


def test_the_same_mismatch_runs_the_second_time_with_a_warning(ask) -> None:
    answer, executed, _ = ask([[call(I02_SQL)], [call(I02_SQL)],
                               [TextBlock(text="退款 35.7 万，GMV 907 万。")]])
    assert answer.status == "ok"
    assert len(executed) == 1
    assert len(answer.warnings) == 1 and answer.warnings[0].startswith(WARNING_PREFIX)
    assert "除法" in answer.warnings[0]
