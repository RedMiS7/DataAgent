"""Scoring the intent and asking back (plans 1.4 and 2.2)."""

from __future__ import annotations

from typing import Any

import pytest
from app.agent.sql_intent import WARNING_PREFIX
from app.api.schemas import AnswerPayload, Clarify, IntentInfo, TableBlock
from app.evaluation import report, runner
from app.evaluation.runner import Activity, CaseResult, intent_metrics, score_intent

QUESTION = {"id": "c01", "category": "clarify", "question": "上个月退货多少",
            "dataset": "ecommerce_demo", "kind": "data"}
INTENT = IntentInfo(text="时间：上个月；指标：退货率", data={
    "route": "query", "metrics": [{"term": "退货", "name": "退货率"}],
    "time_range": {"text": "上个月"},
    "dimensions": [{"term": "渠道", "field": "safe.channels.name"}],
    "filters": [{"field": "orders.province", "value": "河南"}],
    "result_shape": {"type": "top_n", "n": 1, "order": "asc"}})


class _Pipeline:
    def __init__(self, answer: AnswerPayload) -> None:
        self.answer = answer

    def ask(self, *args: Any, **kwargs: Any) -> AnswerPayload:
        return self.answer


def _answer(status: str = "ok", intent: IntentInfo | None = INTENT) -> AnswerPayload:
    clarify = Clarify(question="你想看哪个？", options=["退货率", "退款金额"]) \
        if status == "clarify" else None
    return AnswerPayload(question="q", status=status, sql="SELECT 1",  # type: ignore[arg-type]
                         table=TableBlock(columns=["a"], rows=[[1]], row_count=1),
                         summary="你想看哪个？" if clarify else "3.9%",
                         time_range={"label": "上个月", "start": "2026-09-01",
                                     "end_exclusive": "2026-10-01"},
                         intent=intent, clarify=clarify)


def _ask(question: dict, answer: AnswerPayload) -> CaseResult:
    return runner.ask_question(None, _Pipeline(answer), question)  # type: ignore[arg-type]


def test_each_expected_field_is_scored() -> None:
    expected = {"route": "query", "metrics": ["退货率"], "time_range": "上个月",
                "dimensions": ["channels.name"], "filters": ["河南"],
                "result_shape": {"type": "top_n", "order": "asc"}}
    assert score_intent(_answer(), expected) == dict.fromkeys(expected, True)
    wrong = {"metrics": ["退款金额"], "time_range": "最近30天", "filters": ["河北"],
             "result_shape": {"n": 10}}
    assert score_intent(_answer(), wrong) == dict.fromkeys(wrong, False)


def test_without_an_intent_every_field_is_wrong() -> None:
    assert score_intent(_answer(intent=None), {"metrics": ["退货率"], "route": "query"}) == {
        "metrics": False, "route": False}


def test_an_unknown_expected_field_is_an_error() -> None:
    with pytest.raises(ValueError, match="不认识的字段"):
        score_intent(_answer(), {"metric": ["退货率"]})


def test_a_case_that_should_be_asked_back_passes_only_when_it_is() -> None:
    question = {**QUESTION, "expect_clarify": True}
    asked = _ask(question, _answer("clarify"))
    assert asked.passed and asked.expect_clarify is True
    assert "退货率、退款金额" in asked.detail
    missed = _ask(question, _answer())
    assert not missed.passed and missed.failure_hint == runner.MISSED_CLARIFY


def test_asking_back_when_not_expected_fails() -> None:
    question = {**QUESTION, "category": "metric", "expected_sql": "SELECT 1"}
    case = _ask(question, _answer("clarify"))
    assert not case.passed and case.failure_hint == runner.EXTRA_CLARIFY


def test_a_clarify_case_that_should_be_answered_passes_when_answered() -> None:
    question = {**QUESTION, "expect_clarify": False}
    assert _ask(question, _answer()).passed
    failed = _ask(question, _answer("error"))
    assert not failed.passed and failed.failure_hint == "状态为 error"


def test_a_case_that_should_not_be_asked_back_is_scored_as_usual() -> None:
    question = {**QUESTION, "category": "metric", "expect_clarify": False,
                "expected_intent": {"metrics": ["退货率"]}}
    case = _ask(question, _answer())
    # No reference SQL: it runs and is kept, but counts in no score.
    assert case.scored is False
    assert case.expect_clarify is False
    assert case.intent_fields == {"metrics": True}
    assert case.intent["text"] == INTENT.text


def test_router_calls_are_read_from_the_archive() -> None:
    calls = [{"kind": "model", "agent": "router", "name": "m", "duration_ms": 1, "ok": True},
             {"kind": "model", "agent": "router", "name": "m", "duration_ms": 1, "ok": True},
             {"kind": "model", "agent": "dataagent", "name": "m", "duration_ms": 1, "ok": True}]
    assert Activity.from_archive([], calls).router_calls == 2


def _case(status: str, *, expect: bool | None = None, intent: bool = True, router: int = 1,
          fields: dict[str, bool] | None = None) -> CaseResult:
    return CaseResult(id="x", category="clarify", question="q", passed=True, status=status,
                      expect_clarify=expect, intent={"text": "t"} if intent else {},
                      router_calls=router, intent_fields=fields or {})


def test_intent_metrics_count_fields_missed_and_extra_questions() -> None:
    figures = intent_metrics([
        _case("clarify", expect=True, fields={"metrics": True}),
        _case("ok", expect=True, fields={"metrics": False, "time_range": True}),
        _case("clarify", expect=False),
        _case("ok"),
        _case("ok", intent=False, router=2),
        _case("refused", router=0),
    ])
    assert figures["fields"] == {"metrics": {"right": 1, "total": 2},
                                 "time_range": {"right": 1, "total": 1}}
    assert (figures["clarify_expected"], figures["clarify_missed"]) == (2, 1)
    assert (figures["clarify_unexpected_total"], figures["clarify_extra"]) == (3, 1)
    assert (figures["read"], figures["no_intent"], figures["retried"]) == (5, 1, 1)


def test_sql_turned_back_or_warned_about_is_counted() -> None:
    cases = [_case("ok"), _case("ok")]
    cases[0].intent_blocks, cases[0].intent_warnings = 2, 1
    figures = intent_metrics(cases)
    assert (figures["sql_blocked"], figures["sql_blocked_cases"], figures["sql_warned"]) == (2, 1, 1)


def test_the_answer_warnings_from_the_sql_check_are_counted() -> None:
    answer = _answer()
    answer.warnings = [f"{WARNING_PREFIX}：没有做除法。", "查询用的日期和显示的时间范围不一致"]
    assert _ask({**QUESTION, "expect_clarify": False}, answer).intent_warnings == 1


def test_the_report_prints_the_intent_figures(capsys: pytest.CaptureFixture[str]) -> None:
    report.print_intent(intent_metrics([_case("clarify", expect=True,
                                              fields={"metrics": True}), _case("ok")]))
    printed = capsys.readouterr().out
    assert "字段准确率：指标 1/1（100%）" in printed
    assert "漏问率（该反问的题没反问）：0/1（0%）" in printed
    assert "多问率（不该反问的题反问了）：0/1（0%）" in printed
    assert "SQL 和意图对照：拦下 0 次（0 题），第二次不一致放行并警告 0 次" in printed


def test_the_history_sent_back_carries_the_intent() -> None:
    reply = runner.reply_history(_answer("clarify"))
    assert reply.status == "clarify"
    assert reply.intent == INTENT.data
    assert runner.reply_history(_answer(intent=None)).intent is None
