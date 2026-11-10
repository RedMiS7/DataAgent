from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import cast

import pytest
from app.agent import tools
from app.agent.result_checks import EMPTY_RESULT, compare_group_total, inspect_result
from app.api.schemas import IntentInfo
from app.config import Settings
from app.nl2sql.executor import ExecutionResult, SQLExecutionError
from app.safety.validator import SafeSQL
from app.semantic.retriever import SemanticRetriever

GROUPED = "SELECT region, SUM(quantity) AS 件数 FROM safe.events GROUP BY region"


def _inspect(sql: str, columns: list[str], rows: list[list], *, expects_data: bool = True,
             truncated: bool = False) -> list[str]:
    result = ExecutionResult(columns=columns, rows=rows, truncated=truncated)
    return inspect_result(sql, result, "mysql", expects_data=expects_data, can_check_total=True)[0]


@pytest.mark.parametrize(("expression", "name", "value", "warns"), [
    ("ROUND(SUM(accepted = 1) / NULLIF(COUNT(*), 0) * 100, 1)", "p", 110, True),
    ("ROUND(100.00 * SUM(accepted = 1) / NULLIF(COUNT(*), 0), 1)", "p", 100, False),
    ("COUNT(CASE WHEN accepted = 1 THEN id END) / COUNT(*)", "p", 1.5, True),
    ("AVG(CASE WHEN accepted = 1 THEN 1 ELSE 0 END)", "p", -0.1, True),
    ("SUM(accepted)/COUNT(*)", "通过率", 1.2, True),
    ("SUM(accepted)/COUNT(*)", "通过率", 1, False),
    ("SUM(accepted)/COUNT(*)", "通过率", None, False),
    ("SUM(amount) / SUM(spend)", "ROI", 3.2, False),
    ("(SUM(amount) - SUM(spend)) / SUM(spend) * 100", "毛利率", -25, False),
    ("SUM(current_amount) / SUM(previous_amount) * 100", "同比", 180, False),
    ("SUM(refund_amount) / SUM(paid_amount)", "退款率", 1.5, False),
])
def test_ranges_only_apply_to_bounded_shares(
        expression: str, name: str, value: float | None, warns: bool) -> None:
    sql = f"SELECT {expression} AS {name} FROM safe.events"
    warnings = _inspect(sql, [name], [[value]])
    assert bool(warnings) is warns
    if warns:
        assert "整体份额" in warnings[0]


@pytest.mark.parametrize(("expression", "name", "warns"), [
    ("SUM(quantity)", "qty", True),
    ("SUM(cost)", "总金额", True),
    ("SUM(quantity * unit_price)", "销售额", True),
    ("SUM(quantity * net_price)", "销售额", False),
    ("SUM(net_amount)", "金额", False),
    ("SUM(amount)", "调整金额", False),
    ("SUM(quantity)", "销量增量", False),
    ("SUM(amount) - SUM(spend)", "金额", False),
    ("COUNT(*)", "n", True),
])
def test_negative_values_warn_only_for_simple_counts_and_amounts(
        expression: str, name: str, warns: bool) -> None:
    assert bool(_inspect(f"SELECT {expression} FROM safe.events", [name], [[-1]])) is warns
    assert _inspect(f"SELECT {expression} FROM safe.events", [name], [[0]]) == []


def test_empty_rows_require_data_intent_and_are_distinct_from_zero_and_null() -> None:
    sql = "SELECT COUNT(*) AS count FROM safe.events"
    assert _inspect(sql, ["count"], []) == [EMPTY_RESULT]
    assert _inspect(sql, ["count"], [], expects_data=False) == []
    assert _inspect(sql, ["count"], [[0]]) == []
    assert _inspect(sql, ["count"], [[None]]) == []


def test_group_total_query_retains_filters_and_joins() -> None:
    sql = """
    WITH events AS (SELECT * FROM raw_events WHERE active = 1)
    SELECT t.region, SUM(e.amount) AS amount, COUNT(*) AS n,
           COUNT(DISTINCT e.owner) AS distinct_owners, ROUND(SUM(e.amount), 0) AS rounded_amount
    FROM events e JOIN teams t ON t.id = e.team_id
    WHERE e.day >= '2026-09-01' AND e.day < '2026-10-01'
    GROUP BY t.region ORDER BY n DESC
    """
    with sqlite3.connect(":memory:") as conn:
        conn.executescript("""
            CREATE TABLE raw_events(team_id, amount, owner, day, active);
            CREATE TABLE teams(id, region);
            INSERT INTO teams VALUES (1, '甲'), (2, '乙');
            INSERT INTO raw_events VALUES
                (1, 5.25, 1, '2026-09-01', 1), (1, 7.5, 1, '2026-09-07', 1),
                (2, 3.25, 1, '2026-09-30', 1), (2, 90, 2, '2026-10-01', 1),
                (1, 80, 2, '2026-09-06', 0), (9, 70, 2, '2026-09-07', 1);
        """)
        cursor = conn.execute(sql)
        grouped = ExecutionResult(columns=[column[0] for column in cursor.description],
                                  rows=[list(row) for row in cursor])
        warnings, check = inspect_result(sql, grouped, "sqlite", expects_data=True,
                                         can_check_total=True)
        assert warnings == [] and check is not None
        assert check.measures == ((1, False), (2, True))
        overall = ExecutionResult(rows=[list(row) for row in conn.execute(check.sql)])
    assert overall.rows == [[16.0, 3]]
    assert compare_group_total(grouped, overall, check) == []
    overall.rows[0][1] = 4
    warnings = compare_group_total(grouped, overall, check)
    assert len(warnings) == 1 and "分组合计为 3" in warnings[0] and "总数为 4" in warnings[0]


@pytest.mark.parametrize(("sql", "truncated", "can_check"), [
    (GROUPED + " LIMIT 10", False, True),
    (GROUPED + " LIMIT 10 OFFSET 1", False, True),
    (GROUPED + " HAVING SUM(quantity) > 1", False, True),
    (GROUPED + " WITH ROLLUP", False, True),
    (GROUPED.replace("SUM(quantity)", "COUNT(DISTINCT quantity)"), False, True),
    (GROUPED.replace("SUM(quantity)", "SUM(DISTINCT quantity)"), False, True),
    (GROUPED.replace("SUM(quantity)", "AVG(quantity)"), False, True),
    (GROUPED, True, True),
    (GROUPED, False, False),
])
def test_group_total_skips_incomplete_or_nonadditive_results(
        sql: str, truncated: bool, can_check: bool) -> None:
    result = ExecutionResult(columns=["region", "件数"], rows=[["甲", 3]], truncated=truncated)
    assert inspect_result(sql, result, "mysql", expects_data=True, can_check_total=can_check)[1] is None


def _context(shape: str = "group", max_rows: int = 500) -> tools.AgentRunContext:
    return tools.AgentRunContext(
        settings=Settings(_env_file=None, sql_max_rows=max_rows),
        retriever=cast(SemanticRetriever, None),
        intent=IntentInfo(data={"route": "query", "result_shape": {"type": shape}}),
    )


def _mock_queries(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> list[int]:
    total_timeouts: list[int] = []

    def execute(settings: Settings, safe: SafeSQL) -> ExecutionResult:
        if "check_total_" in safe.sql:
            total_timeouts.append(settings.sql_timeout_ms)
            if fail:
                raise SQLExecutionError("模拟总数查询超时", safe.sql)
            return ExecutionResult(columns=["check_total_1"], rows=[[8]], sql=safe.sql)
        return ExecutionResult(columns=["region", "件数"], rows=[["甲", 2], ["乙", 3]], sql=safe.sql)

    monkeypatch.setattr(tools, "execute_sql", execute)
    return total_timeouts


def test_group_check_is_traced_archived_and_sends_one_model_warning(
        monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts = _mock_queries(monkeypatch)
    context = _context()

    async def run() -> list[dict]:
        first = tools._make_run_sql(context)
        second = tools._make_run_sql(context)
        return [json.loads(reply) for reply in await asyncio.gather(first(GROUPED), second(GROUPED))]

    replies = asyncio.run(run())
    assert timeouts == [tools.GROUP_TOTAL_TIMEOUT_MS]
    assert {reply["query_index"] for reply in replies} == {1, 2}
    warned = [reply for reply in replies if "warnings" in reply]
    assert len(warned) == 1 and "同口径总数为 8" in warned[0]["warnings"][0]
    assert context.result_warnings[warned[0]["query_index"]] == warned[0]["warnings"]
    assert context.attempts == context.reserved == 3 and len(context.executions) == 2
    assert len(context.trace.ordered()) == 3
    assert len([row for row in context.query_records
                if row.status == "ok" and row.query_no is None
                and row.purpose.endswith("分组合计")]) == 1


def test_total_query_failure_leaves_the_primary_result_available(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_queries(monkeypatch, fail=True)
    context = _context()
    reply = json.loads(asyncio.run(tools._make_run_sql(context)(GROUPED)))
    assert reply["rows"] == [["甲", 2], ["乙", 3]]
    assert "warnings" not in reply and not context.errors
    assert context.attempts == 2 and context.reserved == 1
    assert [(record.status, record.query_no) for record in context.query_records] == [
        ("ok", 1), ("failed", None)]
    assert "模拟总数查询超时" in context.query_records[1].error


@pytest.mark.parametrize(("shape", "max_rows", "step_limit"), [
    ("top_n", 500, None), ("group", 2, None), ("group", 500, 1),
])
def test_group_check_respects_the_intent_row_cap_and_step_budget(
        monkeypatch: pytest.MonkeyPatch, shape: str, max_rows: int, step_limit: int | None) -> None:
    timeouts = _mock_queries(monkeypatch)
    context = _context(shape, max_rows)
    reply = json.loads(asyncio.run(tools._make_run_sql(context, step_limit=step_limit)(GROUPED)))
    assert reply["query_index"] == 1 and not timeouts
    assert context.attempts == context.reserved == 1


def test_zero_rows_warning_reaches_the_tool_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def execute(settings: Settings, safe: SafeSQL) -> ExecutionResult:
        return ExecutionResult(columns=["region"], sql=safe.sql)

    monkeypatch.setattr(tools, "execute_sql", execute)
    context = _context("detail")
    reply = json.loads(asyncio.run(tools._make_run_sql(context)("SELECT region FROM safe.events")))
    assert reply["rows"] == [] and reply["warnings"] == [EMPTY_RESULT]
    assert context.query_records[0].status == "ok"
