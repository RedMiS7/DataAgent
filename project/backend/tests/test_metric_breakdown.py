from __future__ import annotations

import asyncio
import sqlite3
from datetime import date
from typing import Any

import pytest
from app.agent import tools
from app.analysis.metric_breakdown import (
    BreakdownError,
    BreakdownPeriod,
    MetricPart,
    build_breakdown_sql,
)
from app.config import Settings
from app.nl2sql.executor import ExecutionResult
from app.nl2sql.time_parser import TimeRange, parse_time_range
from app.safety.validator import SafeSQL, SQLPolicy, validate_and_limit

AS_OF = date(2026, 10, 31)
CURRENT = parse_time_range("上个月", AS_OF)
LOANS = [
    ("A", 16, "2026-09-01"),
    ("A", 18, "2026-09-12"),
    ("A", 12, "2026-09-30"),
    ("B", 9, "2026-09-25"),
    ("A", 13, "2026-08-04"),
    ("C", 15, "2026-08-10"),
    ("C", 10, "2026-08-29"),
    (None, 14, "2026-09-21"),
    ("A", 20, "2026-10-01"),
]
PART = MetricPart(from_sql="main.loan_applications l", expression="COUNT(*)",
                  time_column="l.application_date", dimension="l.loan_type")
HIGH_INTEREST = PART.model_copy(update={"where": "l.interest_rate > 12"})
POLICY = SQLPolicy(schema="main", dialect="sqlite",
                   columns={"loan_applications": ["loan_type", "interest_rate", "application_date"]})


def _execute(settings: Settings, safe: SafeSQL) -> ExecutionResult:
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE loan_applications (loan_type, interest_rate, application_date)")
        conn.executemany("INSERT INTO loan_applications VALUES (?, ?, ?)", LOANS)
        cursor = conn.execute(safe.sql)
        return ExecutionResult(columns=[item[0] for item in cursor.description],
                               rows=[list(row) for row in cursor], sql=safe.sql)


def _result(numerator: MetricPart, denominator: MetricPart | None = None,
            period: TimeRange = CURRENT) -> ExecutionResult:
    sql = build_breakdown_sql(numerator, denominator, None, None, period, "sqlite")
    return _execute(Settings(_env_file=None), validate_and_limit(sql, 500, POLICY))


def test_custom_high_interest_definition_matches_reference_sql() -> None:
    result = _result(HIGH_INTEREST)
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE loan_applications (loan_type, interest_rate, application_date)")
        conn.executemany("INSERT INTO loan_applications VALUES (?, ?, ?)", LOANS)
        reference = conn.execute("""
            SELECT loan_type,
                   SUM(CASE WHEN application_date >= '2026-09-01' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN application_date < '2026-09-01' THEN 1 ELSE 0 END)
            FROM loan_applications
            WHERE interest_rate > 12
              AND application_date >= '2026-08-01' AND application_date < '2026-10-01'
            GROUP BY loan_type
        """).fetchall()
    assert result.rows[0][:6] == ["总体", None, 3, 2, 1, None]
    assert {tuple(row[1:4]) for row in result.rows[1:]} == set(reference)
    assert result.rows[1:] == [
        ["分组", None, 1, 0, 1, 100.0, 3],
        ["分组", "A", 2, 1, 1, 100.0, 3],
        ["分组", "C", 0, 1, -1, -100.0, 3],
    ]


def test_ratio_parts_are_aggregated_independently_and_zero_denominator_is_undefined() -> None:
    result = _result(HIGH_INTEREST, PART)
    overall, *groups = result.rows
    assert overall == ["总体", None, 3, 2, 1, None, 5, 3, 2, None, 60.0, 66.6667, -6.6667, 4]
    by_type = {row[1]: row for row in groups}
    assert by_type["A"] == ["分组", "A", 2, 1, 1, 100.0, 3, 1, 2, 100.0, 66.6667, 100, -33.3333, 4]
    assert by_type["C"] == ["分组", "C", 0, 1, -1, -100.0, 0, 2, -2, -100.0, None, 50.0, None, 4]
    assert by_type["B"][10:13] == [0, None, None]
    assert [row[1] for row in groups][:2] == [None, "A"]


def test_no_net_change_and_nonadditive_group_totals_have_no_contribution() -> None:
    # Different dimensions can contain the same counted entity.
    distinct = PART.model_copy(update={
        "expression": "COUNT(DISTINCT CASE WHEN l.interest_rate > 12 THEN 1 "
                      "WHEN l.interest_rate = 12 THEN 2 END)",
    })
    result = _result(distinct)
    assert result.rows[0][2:5] == [2, 1, 1]
    assert all(row[5] is None for row in result.rows)
    constant = PART.model_copy(update={"expression": "MIN(1)"})
    assert all(row[5] is None for row in _result(constant).rows)


@pytest.mark.parametrize("aggregate", ["AVG", "MIN", "MAX"])
def test_nonadditive_values_stay_null_when_a_group_has_no_rows(
        aggregate: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{__name__}.LOANS", [
        ("A", 12, "2026-08-10"),
        ("B", 24, "2026-09-10"),
    ])
    part = PART.model_copy(update={"expression": f"{aggregate}(l.interest_rate)"})
    result = _result(part)
    assert result.rows[0][2:6] == [24, 12, 12, None]
    assert {row[1]: row[2:6] for row in result.rows[1:]} == {
        "A": [None, 12, None, None],
        "B": [24, None, None, None],
    }
    ratio = {row[1]: row for row in _result(PART, part).rows[1:]}
    assert ratio["A"][6:13] == [None, 12, None, None, None, 8.3333, None]
    assert ratio["B"][6:13] == [24, None, None, None, 4.1667, None, None]


def test_distinct_counts_have_no_contribution_even_when_totals_reconcile() -> None:
    part = PART.model_copy(update={"expression": "COUNT(DISTINCT l.interest_rate)"})
    result = _result(part)
    assert result.rows[0][2:5] == [5, 3, 2]
    assert all(row[5] is None for row in result.rows)


def test_additive_sum_keeps_zero_for_an_empty_period() -> None:
    part = PART.model_copy(update={"expression": "SUM(l.interest_rate)"})
    current = parse_time_range("2026年8月", AS_OF)
    result = _result(part, period=current)
    assert result.rows[0][2:6] == [38, 0, 38, None]
    assert all(row[3] == 0 for row in result.rows)


def test_decreases_rank_by_contribution_in_the_direction_of_change() -> None:
    later = TimeRange(date(2026, 10, 1), date(2026, 11, 1), "十月")
    result = _result(PART, period=later)
    assert result.rows[0][2:5] == [1, 5, -4]
    assert result.rows[1][1:6] == ["A", 1, 3, -2, 50]
    assert [row[5] for row in result.rows[1:]] == [50, 25, 25]


def test_named_comparison_and_explicit_periods() -> None:
    period = parse_time_range("上个月比去年高吗", AS_OF)
    implicit = build_breakdown_sql(PART, None, None, None, period, "sqlite")
    assert "'2025-01-01'" in implicit and "'2026-01-01'" in implicit
    prior = BreakdownPeriod(start=date(2026, 7, 1), end_exclusive=date(2026, 8, 1))
    explicit = build_breakdown_sql(PART, None, None, prior, period, "sqlite")
    assert "'2026-07-01'" in explicit and "'2025-01-01'" not in explicit
    with pytest.raises(BreakdownError, match="必须早于"):
        build_breakdown_sql(PART, None, prior.model_copy(update={"start": prior.end_exclusive}),
                            prior, None, "sqlite")


@pytest.mark.parametrize(("field", "value"), [
    ("from_sql", "main.loan_applications; DELETE FROM loan_applications"),
    ("from_sql", "other.mb_values"),
    ("from_sql", "main.loan_applications l LIMIT 1"),
    ("where", "1 = 1 UNION SELECT 1"),
    ("dimension", "loan_type, application_date"),
    ("expression", "COUNT(*) OVER ()"),
    ("expression", "(SELECT COUNT(*) FROM loan_applications)"),
])
def test_fragments_cannot_change_the_generated_query(field: str, value: str) -> None:
    with pytest.raises(BreakdownError):
        build_breakdown_sql(PART.model_copy(update={field: value}), None, None, None,
                            CURRENT, "sqlite")


def _tool_context(*, visible: bool, max_rows: int = 500) -> tools.AgentRunContext:
    settings = Settings(_env_file=None, mysql_business_db="main", sql_dialect="sqlite",
                        sql_max_rows=max_rows)
    return tools.AgentRunContext(settings=settings, retriever=None, mode="analysis",
                                 time_range=CURRENT,
                                 visible_tables={"loan_applications"} if visible else set())


def test_breakdown_uses_the_same_safety_checks_budget_and_archive(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "sql_policy", lambda settings: POLICY)
    monkeypatch.setattr(tools, "execute_sql", _execute)
    context = _tool_context(visible=False, max_rows=2)

    async def run() -> None:
        toolkit = tools.build_toolkit(context, step_id=2, step_limit=1)
        breakdown = await toolkit.get_tool("metric_breakdown")
        ordinary = await toolkit.get_tool("run_sql")
        assert breakdown and ordinary
        arguments = {"numerator": HIGH_INTEREST.model_dump(), "purpose": "按贷款类型拆解"}
        blocked = await breakdown.call(**arguments)
        assert "还没有检索过" in blocked.content[0].text
        assert context.query_records[0].status == "blocked"
        context.visible_tables.add("loan_applications")
        result = await breakdown.call(**arguments)
        assert '"query_index": 1' in result.content[0].text
        assert '"truncated": true' in result.content[0].text
        assert context.reserved == 1 and context.step_ids == [2]
        record = context.query_records[1]
        assert (record.status, record.query_no, record.row_count) == ("ok", 1, 2)
        assert record.rows == context.executions[0].rows
        assert len(context.trace.ordered()) == 1
        exhausted = await ordinary.call(sql="SELECT 1")
        assert "次数已用完" in exhausted.content[0].text

    asyncio.run(run())


def test_breakdown_and_ordinary_sql_share_the_budget_when_called_together(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "sql_policy", lambda settings: POLICY)
    monkeypatch.setattr(tools, "execute_sql", _execute)
    context = _tool_context(visible=True)

    async def run() -> None:
        toolkit = tools.build_toolkit(context, step_id=2, step_limit=1)
        breakdown = await toolkit.get_tool("metric_breakdown")
        ordinary = await toolkit.get_tool("run_sql")
        assert breakdown and ordinary
        replies = await asyncio.gather(
            breakdown.call(numerator=HIGH_INTEREST.model_dump()),
            ordinary.call(sql="SELECT 1"),
        )
        assert sum("次数已用完" in reply.content[0].text for reply in replies) == 1

    asyncio.run(run())
    assert context.reserved == 1 and len(context.executions) == 1


@pytest.mark.parametrize(("source", "reason"), [
    ("other.loan_applications l", "未授权的库"),
    ("main.hidden l", "未授权的表"),
])
def test_breakdown_cannot_access_unauthorized_sources(
        monkeypatch: pytest.MonkeyPatch, source: str, reason: str) -> None:
    monkeypatch.setattr(tools, "sql_policy", lambda settings: POLICY)
    context = _tool_context(visible=True)

    async def run() -> Any:
        tool = await tools.build_toolkit(context).get_tool("metric_breakdown")
        assert tool is not None
        return await tool.call(numerator=PART.model_copy(update={"from_sql": source}).model_dump())

    reply = asyncio.run(run())
    assert reason in reply.content[0].text
    assert context.query_records[0].status == "blocked"
    assert not context.executions
