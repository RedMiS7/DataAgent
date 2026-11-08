from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from app.agent import tools
from app.api.schemas import AnswerPayload, Failure, HistoryMessage, TableBlock
from app.nl2sql.executor import SQLExecutionError
from app.safety.validator import SQLValidationError

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from eval import runner  # noqa: E402
from eval.runner import (  # noqa: E402
    CaseResult,
    EvalReport,
    ToolProbe,
    compare_rows_loose,
    failure_hint,
    reply_history,
    report_metrics,
)


def test_loose_compare_allows_extra_columns_in_any_order() -> None:
    expected = [["直播", 120.5], ["搜索", 80]]
    actual = [[80.004, "搜索", 3], [5, "直播", 120.5]]
    assert compare_rows_loose(expected, actual)


def test_loose_compare_needs_every_value_and_the_same_rows() -> None:
    assert not compare_rows_loose([["直播", 120.5]], [["直播", 99]])
    assert not compare_rows_loose([["直播", 1]], [["直播", 1], ["搜索", 2]])
    # One actual row cannot stand for two expected rows.
    assert not compare_rows_loose([["直播", 1], ["直播", 1]], [["直播", 1, 1], ["搜索", 2, 2]])


def test_loose_compare_matches_each_value_to_its_own_cell() -> None:
    assert not compare_rows_loose([[5, 5]], [[5, 6]])
    assert compare_rows_loose([[5, 5]], [[5, 6, 5]])


def _fake_tools(monkeypatch: pytest.MonkeyPatch, *, invalid: set[str],
                unknown_column: set[str]) -> None:
    def validate(sql: str, max_rows: int) -> str:
        if sql in invalid:
            raise SQLValidationError("字段不存在：orders.amount")
        return sql

    def execute(settings: Any, safe_sql: str) -> str:
        if safe_sql in unknown_column:
            raise SQLExecutionError("(1054, \"Unknown column 'cost' in 'field list'\")", safe_sql)
        return "rows"

    monkeypatch.setattr(tools, "validate_and_limit", validate)
    monkeypatch.setattr(tools, "execute_sql", execute)
    monkeypatch.setattr(tools, "describe_tables", lambda settings, names: "columns")


def test_probe_counts_attempts_failures_and_field_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_tools(monkeypatch, invalid={"bad"}, unknown_column={"cost"})
    with ToolProbe().watching() as probe:
        with pytest.raises(SQLValidationError):
            tools.validate_and_limit("bad", 10)
        tools.validate_and_limit("cost", 10)
        with pytest.raises(SQLExecutionError):
            tools.execute_sql(None, "cost")
        assert tools.describe_tables(None, ["orders"]) == "columns"
        tools.validate_and_limit("good", 10)
        assert tools.execute_sql(None, "good") == "rows"

    assert probe.sql_attempts == 3
    assert probe.sql_failures == 2
    assert probe.field_errors == 2
    assert probe.describe_calls == 1


def test_probe_restores_the_tools_afterwards(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_tools(monkeypatch, invalid=set(), unknown_column=set())
    before = tools.execute_sql
    with ToolProbe().watching():
        assert tools.execute_sql is not before
    assert tools.execute_sql is before


def test_reply_history_mirrors_the_frontend() -> None:
    ok = AnswerPayload(question="q", summary="上个月 GMV 为 100 元。", mode="query")
    assert reply_history(ok) == HistoryMessage(role="assistant", content="上个月 GMV 为 100 元。",
                                               mode="query", status="ok")
    failed = AnswerPayload(question="q", status="error", summary="",
                           failure=Failure(kind="timeout", message="超过 120 秒还没有查到数据。"))
    assert reply_history(failed) == HistoryMessage(
        role="assistant", content="超过 120 秒还没有查到数据。", mode="query", status="error")
    assert reply_history(AnswerPayload(question="q", status="empty", summary="")).content == ""


def _answer(sql: str = "", rows: list[list] | None = None, status: str = "ok") -> AnswerPayload:
    table = TableBlock(columns=["a"], rows=rows or [], row_count=len(rows or []))
    return AnswerPayload(question="q", sql=sql, status=status, table=table)  # type: ignore[arg-type]


def test_failure_hint_names_what_went_wrong() -> None:
    probe = ToolProbe()
    reference = "SELECT 1 FROM safe.orders o WHERE o.paid_at >= '2026-09-01'"
    hint = failure_hint("single_table", _answer(status="error"), reference, [[1]], [], probe)
    assert hint == "没有结果"
    probe.events.append(runner.ProbeEvent("execute", False, "Unknown column 'x'"))
    hint = failure_hint("single_table", _answer(status="error"), reference, [[1]], [], probe)
    assert hint == "字段不存在后未恢复"

    probe = ToolProbe()
    same_dates = "SELECT 1 FROM safe.orders o WHERE o.paid_at >= '2026-09-01'"
    assert failure_hint("single_table", _answer(same_dates, [[1], [2]]), reference, [[1]],
                        [[1], [2]], probe) == "行数不同"
    assert failure_hint("single_table", _answer(same_dates, [[1, 2]]), reference, [[1]],
                        [[1, 2]], probe) == "列数不同"
    other_dates = "SELECT 1 FROM safe.orders o WHERE o.paid_at >= '2026-08-01'"
    assert failure_hint("single_table", _answer(other_dates, [[2]]), reference, [[1]],
                        [[2]], probe) == "日期不同"
    other_table = "SELECT 1 FROM safe.refunds r WHERE r.completed_at >= '2026-09-01'"
    assert failure_hint("single_table", _answer(other_table, [[2]]), reference, [[1]],
                        [[2]], probe) == "用的表不同"
    assert failure_hint("single_table", _answer(same_dates, [[2]]), reference, [[1]],
                        [[2]], probe) == "数值不同"
    assert failure_hint("attribution", _answer(same_dates, [[2]]), "", [], [[2]],
                        probe) == "没有参考答案"
    assert failure_hint("direct", _answer(same_dates, [[2]]), "", [], [[2]],
                        probe) == "没有直接回答"


def test_report_metrics_sum_the_cases() -> None:
    cases = [
        CaseResult(id="1", category="single_table", question="q", passed=True, sql_attempts=1,
                   model_calls=2, input_tokens=100, output_tokens=10, loose_passed=True),
        CaseResult(id="2", category="single_table", question="q", passed=False, sql_attempts=3,
                   sql_failures=2, field_errors=1, describe_calls=1, model_calls=4,
                   input_tokens=300, output_tokens=30, loose_passed=True,
                   failure_hint="列数不同"),
        CaseResult(id="3", category="dangerous_sql", question="q", passed=True),
    ]
    metrics = report_metrics(cases)
    assert metrics["sql_clean_rate"] == 0.5
    assert metrics["questions_with_sql"] == 2
    assert metrics["sql_attempts"] == 4
    assert metrics["sql_failures"] == 2
    assert metrics["field_errors"] == 1
    assert metrics["describe_calls"] == 1
    assert metrics["model_calls_avg"] == 3.0
    assert metrics["input_tokens_avg"] == 200.0
    assert metrics["loose_passed"] == 2 and metrics["loose_total"] == 2
    assert metrics["failure_hints"] == {"列数不同": 1}

    report = EvalReport()
    for case in cases:
        report.add(case)
    assert report.as_dict()["metrics"] == metrics


class _FakePipeline:
    """Answers from a script and keeps what each call received."""

    def __init__(self, answers: dict[str, AnswerPayload]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, list[HistoryMessage]]] = []

    def ask(self, question: str, session_id: str = "default", emit: Any = None,
            mode: str = "query", history: list[HistoryMessage] | None = None) -> AnswerPayload:
        self.calls.append((question, mode, list(history or [])))
        return self.answers[question]


def test_multiturn_case_asks_earlier_turns_and_compares_loosely(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "execute_reference", lambda settings, sql: [["直播", 10.0]])
    pipeline = _FakePipeline({
        "上个月GMV是多少？": AnswerPayload(question="上个月GMV是多少？", summary="GMV 为 30 元。"),
        "按渠道拆一下": _answer("SELECT ...", [["直播", 10.0, 3]]),
    })
    question = {"id": "t01", "category": "multiturn", "match": "loose",
                "turns": ["上个月GMV是多少？"], "question": "按渠道拆一下",
                "expected_sql": "SELECT ..."}

    report = runner.evaluate_questions(None, pipeline, [question])  # type: ignore[arg-type]

    case = report.cases[0]
    assert case.passed and case.loose_passed
    assert "宽松比对通过" in case.detail
    asked, _, history = pipeline.calls[-1]
    assert asked == "按渠道拆一下"
    assert [(item.role, item.content) for item in history] == [
        ("user", "上个月GMV是多少？"), ("assistant", "GMV 为 30 元。")]


def test_strict_case_still_fails_on_extra_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "execute_reference", lambda settings, sql: [["直播", 10.0]])
    pipeline = _FakePipeline({"各渠道GMV": _answer("SELECT ...", [["直播", 10.0, 3]])})
    question = {"id": "m01", "category": "multi_table", "question": "各渠道GMV",
                "expected_sql": "SELECT ..."}

    case = runner.evaluate_questions(None, pipeline, [question]).cases[0]  # type: ignore[arg-type]

    assert not case.passed and case.loose_passed
    assert case.failure_hint == "列数不同"


def test_written_history_is_sent_as_is() -> None:
    pipeline = _FakePipeline({"上个月的订单数是多少？": _answer("SELECT ...", [[5]])})
    question = {"id": "t04", "category": "multiturn", "match": "loose",
                "history_messages": [{"role": "user", "content": "上个月各省份的退款金额是多少？"}],
                "question": "上个月的订单数是多少？", "expected_sql": ""}

    runner.evaluate_questions(None, pipeline, [question])  # type: ignore[arg-type]

    _, _, history = pipeline.calls[0]
    assert [(item.role, item.content) for item in history] == [
        ("user", "上个月各省份的退款金额是多少？")]


def test_direct_case_passes_only_on_a_text_answer_without_sql() -> None:
    pipeline = _FakePipeline({
        "GMV包不包括运费？": AnswerPayload(question="GMV包不包括运费？", status="error"),
    })
    question = {"id": "d01", "category": "direct", "mode": "auto",
                "question": "GMV包不包括运费？", "expected_sql": ""}

    case = runner.evaluate_questions(None, pipeline, [question]).cases[0]  # type: ignore[arg-type]

    assert pipeline.calls[0][1] == "auto"
    assert not case.passed
    assert case.failure_hint == "状态为 error"
    assert runner.score_direct(AnswerPayload.model_construct(question="q", status="text",
                                                             sql=""))[0]


def test_new_cases_are_well_formed() -> None:
    questions = runner.load_questions()
    ids = [item["id"] for item in questions]
    assert len(ids) == len(set(ids))
    for item in questions:
        if item["category"] == "multiturn":
            assert item["expected_sql"] and item.get("match") == "loose"
            assert item.get("turns") or item.get("history_messages")
        if item["category"] == "direct":
            assert item["mode"] == "auto" and not item["expected_sql"]
        for message in item.get("history_messages") or []:
            HistoryMessage(**message)

    routes = runner.load_routing_cases()
    route_ids = [item["id"] for item in routes]
    assert len(route_ids) == len(set(route_ids))
    assert {item["expected_route"] for item in routes} == {"query", "analysis", "direct"}
    json.dumps(routes, ensure_ascii=False)
