"""Columns are checked before SQL runs; a wrong one comes back with what to write instead."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from app.agent import tools
from app.config import Settings
from app.nl2sql.executor import ExecutionResult, SQLExecutionError
from app.safety import validator
from app.safety.validator import (
    SQLPolicy,
    SQLValidationError,
    unknown_column_hint,
    validate_and_limit,
)
from business_schema import COLUMNS, POLICY
from sqlglot.errors import OptimizeError

PROJECT_DIR = Path(__file__).resolve().parents[2]


def _sql_that_ran() -> list[str]:
    """Every SQL the evaluations ran successfully, and every reference answer."""
    found: set[str] = set()
    for path in (PROJECT_DIR / "eval" / "results").glob("*.json"):
        result = json.loads(path.read_text(encoding="utf-8"))
        # Retrieval results hold no SQL.
        for case in result.get("summary", {}).get("cases", []):
            if case.get("sql") and case.get("status") in ("ok", "empty", "partial"):
                found.add(case["sql"])
    for question in json.loads((PROJECT_DIR / "eval" / "questions.json").read_text("utf-8")):
        if question.get("expected_sql"):
            found.add(question["expected_sql"])
    return sorted(found)


def test_no_sql_that_ran_is_blocked() -> None:
    queries = _sql_that_ran()
    assert len(queries) > 100
    blocked = []
    for sql in queries:
        try:
            validate_and_limit(sql, 500, POLICY)
        except SQLValidationError as exc:
            blocked.append((sql, str(exc)))
    assert blocked == []


@pytest.mark.parametrize("sql", [
    "SELECT TIMESTAMPDIFF(HOUR, o.paid_at, s.delivered_at) AS h FROM safe.orders o "
    "JOIN safe.shipments s ON s.order_id = o.id",
    "SELECT DATE_FORMAT(o.paid_at, '%Y-%m') AS m, COUNT(*) FROM safe.orders o GROUP BY m "
    "HAVING COUNT(*) > 1 ORDER BY m",
    "SELECT p.name, SUM(oi.quantity) AS qty, RANK() OVER (ORDER BY SUM(oi.quantity) DESC) AS rk "
    "FROM safe.order_items oi JOIN safe.products p ON p.id = oi.product_id GROUP BY p.name",
    "SELECT * FROM (SELECT o.channel_id, COUNT(*) AS n FROM safe.orders o "
    "GROUP BY o.channel_id) t WHERE t.n > 10",
    "SELECT o.id FROM safe.orders o WHERE o.paid_at >= DATE_SUB('2026-10-31', INTERVAL 30 DAY)",
    "WITH a AS (SELECT o.id, o.total_amount AS amt FROM safe.orders o) SELECT SUM(a.amt) FROM a",
    "SELECT c.name, (SELECT COUNT(*) FROM safe.orders o2 WHERE o2.channel_id = c.id) AS n "
    "FROM safe.channels c",
    "SELECT name, COUNT(*) FROM safe.channels GROUP BY 1",
    "SELECT ORDER_NO, Province FROM safe.orders",
    "SELECT CAST(o.paid_at AS DATE) AS d, COUNT(*) FROM safe.orders o GROUP BY CAST(o.paid_at AS DATE)",
])
def test_valid_sql_passes(sql: str) -> None:
    validate_and_limit(sql, 500, POLICY)


def _rejection(sql: str) -> str:
    with pytest.raises(SQLValidationError) as caught:
        validate_and_limit(sql, 500, POLICY)
    return str(caught.value)


def test_a_wrong_column_gets_the_closest_real_one() -> None:
    message = _rejection("SELECT o.amount FROM safe.orders o")
    assert message.splitlines()[0] == "字段不存在：orders.amount。最接近的字段：orders.total_amount。"
    assert message.splitlines()[1].startswith("orders 的字段：id, order_no, customer_id")


def test_a_column_without_a_table_is_matched_across_the_tables() -> None:
    message = _rejection("SELECT o.id FROM safe.orders o JOIN safe.refunds r ON r.order_id = o.id "
                         "WHERE amount > 1")
    assert message.startswith("字段不存在：amount。最接近的字段：orders.total_amount、refunds.refund_amount")
    assert "orders 的字段：" in message and "refunds 的字段：" in message


def test_a_column_with_no_close_spelling_still_lists_the_real_ones() -> None:
    message = _rejection("SELECT ads.cost FROM safe.ad_daily_stats ads")
    assert message.splitlines()[0] == "字段不存在：ad_daily_stats.cost。"
    assert "spend" in message.splitlines()[1]


def test_an_unknown_alias_names_the_aliases_in_use() -> None:
    message = _rejection("SELECT x.name FROM safe.channels c")
    assert "查询里没有叫 x 的表或别名" in message and "c（channels）" in message


def test_a_wrong_column_of_a_subquery_lists_its_columns() -> None:
    # From an analysis in the evaluation: gmv is in curg, not cur.
    message = _rejection(
        "SELECT cur.refund_amt, cur.gmv FROM (SELECT r.product_id AS pid, "
        "SUM(r.refund_amount) AS refund_amt FROM safe.refunds r GROUP BY r.product_id) cur "
        "JOIN (SELECT oi.product_id AS pid, SUM(oi.quantity) AS gmv FROM safe.order_items oi "
        "GROUP BY oi.product_id) curg ON curg.pid = cur.pid")
    assert message.splitlines()[0] == "字段不存在：cur.gmv，子查询 cur 里没有 gmv 这一列。"
    assert message.splitlines()[1] == "子查询 cur 的列：pid, refund_amt"


def test_a_wrong_column_of_a_cte_lists_its_columns() -> None:
    message = _rejection("WITH t AS (SELECT o.id AS order_id, o.paid_at FROM safe.orders o) "
                         "SELECT t.paid FROM t")
    assert message.startswith("字段不存在：t.paid，子查询 t 里没有 paid 这一列。最接近的列：paid_at。")
    assert message.splitlines()[1] == "子查询 t 的列：order_id, paid_at"


def test_without_columns_the_check_is_skipped() -> None:
    validate_and_limit("SELECT o.amount FROM safe.orders o", 500,
                       SQLPolicy(schema="safe", dialect="mysql"))


@pytest.mark.parametrize("error", [OptimizeError("Unsupported thing"), RuntimeError("bug")])
def test_what_the_check_cannot_judge_is_let_through(monkeypatch: pytest.MonkeyPatch,
                                                    error: Exception) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(validator, "qualify", fail)
    validate_and_limit("SELECT o.amount FROM safe.orders o", 500, POLICY)


def test_mysql_unknown_column_gets_the_same_hint() -> None:
    hint = unknown_column_hint("SELECT o.cost FROM safe.orders AS o", "o.cost", COLUMNS, "mysql")
    assert hint.startswith("字段不存在：orders.cost。")
    assert "orders 的字段：" in hint


# run_sql

GOOD_SQL = "SELECT o.province FROM safe.orders o"
WRONG_SQL = "SELECT o.amount FROM safe.orders o"


def _context(monkeypatch: pytest.MonkeyPatch, execute: Any) -> tools.AgentRunContext:
    monkeypatch.setattr(tools, "known_columns", lambda settings: COLUMNS)
    monkeypatch.setattr(tools, "execute_sql", execute)
    settings = Settings(_env_file=None, sql_max_retry=3)
    return tools.AgentRunContext(settings=settings, retriever=None)  # type: ignore[arg-type]


def _ok(settings: Settings, safe: Any) -> ExecutionResult:
    return ExecutionResult(columns=["province"], rows=[["广东"]], sql=safe.sql)


def test_run_sql_stops_a_wrong_column_before_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[str] = []

    def execute(settings: Settings, safe: Any) -> ExecutionResult:
        executed.append(safe.sql)
        return _ok(settings, safe)

    context = _context(monkeypatch, execute)
    reply = asyncio.run(tools._make_run_sql(context)(WRONG_SQL))
    assert reply.startswith("SQL 校验失败：字段不存在：orders.amount。最接近的字段：orders.total_amount。")
    assert executed == []
    assert context.failed_sql == WRONG_SQL


def test_run_sql_explains_mysql_unknown_column(monkeypatch: pytest.MonkeyPatch) -> None:
    def execute(settings: Settings, safe: Any) -> ExecutionResult:
        raise SQLExecutionError("SQL 执行失败：(1054, \"Unknown column 'o.cost' in 'field list'\")",
                                safe.sql)

    context = _context(monkeypatch, execute)
    # The check lets it through (here: it cannot judge), MySQL rejects it.
    monkeypatch.setattr(validator, "qualify", lambda *args, **kwargs: None)
    reply = asyncio.run(tools._make_run_sql(context)("SELECT o.cost FROM safe.orders o"))
    assert "字段不存在：orders.cost。" in reply
    assert tools.UNKNOWN_COLUMN_HINT not in reply


def test_run_sql_stops_after_failures_in_a_row(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context(monkeypatch, _ok)
    run_sql = tools._make_run_sql(context)

    replies = [asyncio.run(run_sql(WRONG_SQL)) for _ in range(3)]
    assert "不要再调用 run_sql" not in replies[0] and "不要再调用 run_sql" not in replies[1]
    assert "SQL 已经连续失败 3 次" in replies[2]
    assert context.attempts == 3

    # Past the limit nothing runs, not even a correct query.
    assert asyncio.run(run_sql(GOOD_SQL)).startswith("SQL 已经连续失败 3 次")
    assert context.attempts == 3 and context.executions == []


def test_a_query_that_runs_resets_the_count(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context(monkeypatch, _ok)
    run_sql = tools._make_run_sql(context)
    for sql in (WRONG_SQL, WRONG_SQL, GOOD_SQL, WRONG_SQL, WRONG_SQL):
        reply = asyncio.run(run_sql(sql))
    assert "不要再调用 run_sql" not in reply
    assert len(context.executions) == 1
