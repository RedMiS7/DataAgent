"""Scoring rules of plan 1.4: rounding, loose rows as the score, cases with nothing to
score by, analysis figures, and fact checkpoints."""

from __future__ import annotations

from typing import Any

import pytest
from app.api.schemas import AnswerPayload, QueryResult, TableBlock
from app.evaluation import runner
from app.evaluation.runner import CaseResult, EvalReport, compare_rows, compare_rows_loose


class _Pipeline:
    def __init__(self, answer: AnswerPayload) -> None:
        self.answer = answer

    def ask(self, question: str, session_id: str = "default", emit: Any = None,
            mode: str = "query", history: Any = None) -> AnswerPayload:
        return self.answer


def _answer(rows: list[list], summary: str = "", status: str = "ok") -> AnswerPayload:
    table = TableBlock(columns=["a"] * len(rows[0]) if rows else ["a"], rows=rows,
                       row_count=len(rows))
    return AnswerPayload(question="q", sql="SELECT ...", status=status, table=table,  # type: ignore[arg-type]
                         summary=summary, summary_source="model" if summary else "template")


def _ask(question: dict, answer: AnswerPayload) -> CaseResult:
    return runner.ask_question(None, _Pipeline(answer), question)  # type: ignore[arg-type]


@pytest.mark.parametrize(("expected", "actual", "same"), [
    (12.3, 12.345, True),       # rounded to the reference's one decimal
    (1234.5, 1234.46, True),
    (0.1235, 0.12345, True),    # half rounds up
    (12.35, 12.3, False),       # fewer decimals than the reference is not rounding
    (100, 100.4, True),
    (100, 100.6, False),
    (9072501.5, 9072501.5, True),
    ("直播", "直播", True),
    ("直播", "搜索", False),
    ("12.3", 12.345, True),     # numbers as text count as numbers
])
def test_values_are_compared_at_the_reference_decimals(expected: Any, actual: Any,
                                                       same: bool) -> None:
    assert runner._cell_matches(expected, actual) is same
    assert compare_rows([[expected]], [[actual]]) is same
    assert compare_rows_loose([[expected]], [[actual]]) is same


def test_a_question_without_reference_or_facts_runs_but_is_not_scored() -> None:
    question = {"id": "a01", "category": "attribution", "question": "退货率为什么涨了？",
                "dataset": "ecommerce_demo", "kind": "data", "expected_sql": ""}

    case = _ask(question, _answer([[1]], "退货率涨了 2%。"))

    assert not case.scored and case.unscored_reason == "没有判分依据"
    assert case.summary == "退货率涨了 2%。" and case.failure_hint == "没有判分依据"
    report = EvalReport([case, CaseResult(id="q01", category="metric", question="q",
                                          passed=True, dataset="ecommerce_demo", kind="data")])
    summary = report.summary()
    assert summary["groups"]["ecommerce_demo/data"]["total"] == 1
    assert summary["unscored"] == ["a01"]


def test_unverified_figures_no_longer_fail_an_analysis() -> None:
    answer = AnswerPayload(question="q", status="ok", summary="GMV 涨了 12.5%。",
                           summary_source="model", warnings=["结论里的 12.5% 未能在查询结果中核对"],
                           results=[QueryResult(index=1, purpose="整体 GMV", sql="SELECT 1")])
    passed, detail = runner.score_analysis(answer, {"expected_dimensions": ["整体"]})
    assert passed and "数字未核对" not in detail


def _facts(monkeypatch: pytest.MonkeyPatch, rows: list[list]) -> list[str]:
    ran: list[str] = []

    def reference(settings: Any, sql: str) -> list[list]:
        ran.append(sql)
        return rows

    monkeypatch.setattr(runner, "execute_reference", reference)
    return ran


def test_a_fact_passes_when_the_conclusion_shows_its_values(
        monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _facts(monkeypatch, [["直播", 9072501.5, 0.1234]])
    question = {"id": "a02", "category": "attribution", "question": "为什么涨了？",
                "facts": [{"name": "主要渠道", "sql": "SELECT channel, gmv, share",
                           "text": ["退货"]}]}

    case = _ask(question, _answer([[1]], "直播渠道 GMV 907.25万，占 12.3%，退货也多。"))

    assert ran == ["SELECT channel, gmv, share"]
    assert case.passed and case.scored and "检查点 1/1" in case.detail


def test_a_fact_fails_when_the_conclusion_misses_a_value(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _facts(monkeypatch, [["直播", 9072501.5]])
    question = {"id": "a02", "category": "attribution", "question": "为什么涨了？",
                "facts": [{"name": "主要渠道", "sql": "SELECT channel, gmv", "text": ["退货"]}]}

    case = _ask(question, _answer([[1]], "搜索渠道 GMV 800万。"))

    assert not case.passed and case.failure_hint == "结论缺少检查点"
    assert "主要渠道：结论没有提到 直播、9072501.5、退货" in case.detail


def test_facts_add_to_the_reference_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "execute_reference", lambda settings, sql: [[10.0]])
    question = {"id": "q09", "category": "metric", "question": "GMV？",
                "expected_sql": "SELECT gmv", "facts": [{"text": ["上个月"]}]}

    assert _ask(question, _answer([[10.0]], "上个月 GMV 为 10 元。")).passed
    missed = _ask(question, _answer([[10.0]], "GMV 为 10 元。"))
    assert not missed.passed and missed.loose_passed


def test_a_fact_query_without_rows_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _facts(monkeypatch, [])
    missing = runner.check_facts(None, [{"name": "x", "sql": "SELECT 1"}], "任何结论")  # type: ignore[arg-type]
    assert missing == ["x：检查点 SQL 没有结果"]


@pytest.mark.parametrize(("value", "text", "shown"), [
    (9072501.5, "GMV 9,072,501.5 元", True),
    (9072501.5, "GMV 约 907.3万", True),
    (9072501.5, "GMV 约 908万", False),
    (0.1234, "占比 12.34%", True),
    (12.34, "占比 12.34%", True),
    (-3.5, "下降了 3.5", True),
    (3.5, "没有数字", False),
])
def test_number_is_shown_allows_display_rounding_and_units(value: float, text: str,
                                                           shown: bool) -> None:
    assert runner.number_is_shown(value, text) is shown


def test_repeat_stats_give_each_attempt_and_pass_all() -> None:
    cases = [CaseResult(id=case_id, category="metric", question="q", passed=passed,
                        attempt=attempt, dataset="d", kind="data")
             for case_id, attempt, passed in [("q1", 1, True), ("q2", 1, True),
                                              ("q1", 2, True), ("q2", 2, False),
                                              ("q1", 3, True), ("q2", 3, True)]]
    stats = runner.repeat_stats(cases)
    assert stats["attempts"] == 3
    assert stats["accuracy"] == [1.0, 0.5, 1.0]
    assert (stats["mean"], stats["min"], stats["max"]) == (0.8333, 0.5, 1.0)
    assert stats["pass_all"] == 0.5
    summary = EvalReport(cases).summary()
    assert summary["repeat"]["d/data"] == stats
    assert "repeat" not in EvalReport(cases[:2]).summary()


def test_saved_results_load_back_and_ignore_unknown_keys() -> None:
    from dataclasses import asdict
    case = CaseResult(id="q1", category="metric", question="q", passed=True, attempt=2,
                      warnings=["w"], log_id=5)
    assert CaseResult.from_dict({**asdict(case), "added_later": 1}) == case
