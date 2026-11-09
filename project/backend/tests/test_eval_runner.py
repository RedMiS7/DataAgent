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
    previous_reference,
    reply_history,
    report_metrics,
    shift_dates,
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


def test_reply_history_carries_the_queries_like_the_frontend() -> None:
    period = {"label": "上个月", "start": "2026-09-01", "end_exclusive": "2026-10-01"}
    answer = AnswerPayload(question="上个月GMV是多少？", summary="GMV 为 9 万。", sql="SELECT 1",
                           time_range=period)
    reply = reply_history(answer)
    assert [(query.purpose, query.sql) for query in reply.queries] == [("上个月GMV是多少？",
                                                                        "SELECT 1")]
    assert reply.time_range == period
    failed = AnswerPayload(question="q", status="error", sql="SELECT amount", time_range=period)
    assert reply_history(failed).queries == [] and reply_history(failed).time_range == {}


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
                   model_calls=2, input_tokens=100, output_tokens=10, cached_input_tokens=80,
                   loose_passed=True),
        CaseResult(id="2", category="single_table", question="q", passed=False, sql_attempts=3,
                   sql_failures=2, field_errors=1, describe_calls=1, model_calls=4,
                   input_tokens=300, output_tokens=30, cached_input_tokens=120, loose_passed=True,
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
    assert metrics["cached_input_tokens_avg"] == 100.0
    assert metrics["cache_hit_rate"] == 0.5
    assert metrics["loose_passed"] == 2 and metrics["loose_total"] == 2
    assert metrics["failure_hints"] == {"列数不同": 1}

    report = EvalReport()
    for case in cases:
        report.add(case)
    # The cases carry no dataset or kind, so they fall in one group.
    assert report.as_dict()["groups"]["/"]["metrics"] == metrics


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


SEPT_TO_AUG = {"2026-09-01": "2026-08-01", "2026-10-01": "2026-09-01"}
REFUND_SQL = ("SELECT c.name, SUM(r.refund_amount) FROM safe.refunds r WHERE "
              "r.requested_at >= '2026-09-01' AND r.requested_at < '2026-10-01 00:00:00'")


def test_shift_dates_moves_each_date_once() -> None:
    assert shift_dates(REFUND_SQL, SEPT_TO_AUG) == REFUND_SQL.replace(
        "'2026-09-01'", "'2026-08-01'").replace("'2026-10-01 ", "'2026-09-01 ")


def test_previous_reference_needs_a_result_with_the_dates() -> None:
    ok = AnswerPayload(question="q", sql=REFUND_SQL)
    assert previous_reference(ok, SEPT_TO_AUG) == (shift_dates(REFUND_SQL, SEPT_TO_AUG), "")
    assert previous_reference(None, SEPT_TO_AUG) == ("", "上一轮没有查出结果")
    failed = AnswerPayload(question="q", status="error", sql=REFUND_SQL)
    assert previous_reference(failed, SEPT_TO_AUG) == ("", "上一轮没有查出结果")
    other = AnswerPayload(question="q", sql="SELECT 1 FROM safe.orders WHERE d >= '2026-07-01'")
    assert previous_reference(other, SEPT_TO_AUG) == ("", "上一轮 SQL 里没有要替换的日期")


def _consistency_case() -> dict:
    return {"id": "c01", "category": "consistency", "match": "loose",
            "turns": ["上个月按退款申请时间算，各渠道的退款金额是多少？"],
            "question": "那上上个月呢？", "expected_sql": "",
            "expect_from_previous": SEPT_TO_AUG}


def test_a_consistency_case_compares_with_the_earlier_sql_moved(
        monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    def reference(settings: Any, sql: str) -> list[list]:
        ran.append(sql)
        return [["直播", 10.0]]

    monkeypatch.setattr(runner, "execute_reference", reference)
    first = AnswerPayload(question="上个月按退款申请时间算，各渠道的退款金额是多少？",
                          summary="直播 12 元。", sql=REFUND_SQL)
    pipeline = _FakePipeline({first.question: first,
                              "那上上个月呢？": _answer("SELECT completed_at ...", [["直播", 9.0]])})

    case = runner.evaluate_questions(None, pipeline, [_consistency_case()]).cases[0]  # type: ignore[arg-type]

    assert ran == [shift_dates(REFUND_SQL, SEPT_TO_AUG)]
    assert case.reference_sql == ran[0]
    assert not case.passed
    # The earlier SQL reached the model through the history.
    assert pipeline.calls[-1][2][1].queries[0].sql == REFUND_SQL


def test_the_history_can_leave_out_the_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "execute_reference", lambda settings, sql: [["直播", 9.0]])
    first = AnswerPayload(question="上个月按退款申请时间算，各渠道的退款金额是多少？",
                          summary="直播 12 元。", sql=REFUND_SQL)
    pipeline = _FakePipeline({first.question: first,
                              "那上上个月呢？": _answer("SELECT ...", [["直播", 9.0]])})

    case = runner.evaluate_questions(None, pipeline, [_consistency_case()],  # type: ignore[arg-type]
                                     history_sql=False).cases[0]

    assert case.passed
    assert pipeline.calls[-1][2][1].queries == []


def test_a_consistency_case_without_an_earlier_result_fails_clearly(
        monkeypatch: pytest.MonkeyPatch) -> None:
    first = AnswerPayload(question="上个月按退款申请时间算，各渠道的退款金额是多少？",
                          status="error")
    pipeline = _FakePipeline({first.question: first,
                              "那上上个月呢？": _answer("SELECT ...", [["直播", 9.0]])})

    case = runner.evaluate_questions(None, pipeline, [_consistency_case()]).cases[0]  # type: ignore[arg-type]

    assert not case.passed and case.failure_hint == "上一轮没有查出结果"


def test_consistency_cases_are_well_formed() -> None:
    cases = [item for item in runner.load_questions() if item["category"] == "consistency"]
    assert len(cases) >= 5
    for item in cases:
        assert item["turns"] and item["match"] == "loose" and not item["expected_sql"]
        assert all(len(key) == 10 and len(value) == 10
                   for key, value in item["expect_from_previous"].items())



def _case(case_id: str, category: str, kind: str, passed: bool,
          dataset: str = "ecommerce_demo", scored: bool = True) -> CaseResult:
    return CaseResult(id=case_id, category=category, question="q", passed=passed,
                      dataset=dataset, kind=kind, scored=scored)


def test_report_scores_each_dataset_and_kind_apart() -> None:
    report = EvalReport()
    for case in [
        _case("q01", "single_table", "data", True),
        _case("f01", "followup", "data", False),
        # Same category name as the data question above, but a routing case.
        _case("rt21", "followup", "routing", True),
        _case("s01", "dangerous_sql", "rule", True),
        _case("dab-48", "attribution", "data", True, dataset="dabench"),
        _case("dab-101", "attribution", "data", False, dataset="dabench", scored=False),
    ]:
        report.add(case)

    payload = report.as_dict()
    groups = payload["groups"]
    assert set(groups) == {"ecommerce_demo/data", "ecommerce_demo/routing",
                           "ecommerce_demo/rule", "dabench/data"}
    assert "total" not in payload and "accuracy" not in payload
    data = groups["ecommerce_demo/data"]
    assert (data["passed"], data["total"], data["accuracy"]) == (1, 2, 0.5)
    assert data["by_category"]["followup"] == {"total": 1, "passed": 0}
    assert data["failed"] == ["f01"]
    assert groups["ecommerce_demo/routing"]["by_category"] == {
        "followup": {"total": 1, "passed": 1}}
    # The suspect case runs and is kept, but counts in no group.
    assert groups["dabench/data"]["total"] == 1
    assert payload["unscored"] == ["dab-101"]
    assert [case["id"] for case in payload["cases"]][-1] == "dab-101"


def test_cases_are_picked_by_the_business_database() -> None:
    assert runner.dataset_for(type("S", (), {"mysql_business_db": "safe"})()) == "ecommerce_demo"
    assert runner.dataset_for(type("S", (), {"mysql_business_db": "dabench"})()) == "dabench"
    with pytest.raises(RuntimeError, match="other"):
        runner.dataset_for(type("S", (), {"mysql_business_db": "other"})())

    cases = [{"id": "a", "dataset": "ecommerce_demo"}, {"id": "b", "dataset": "dabench"}]
    assert [case["id"] for case in runner.select_cases(cases, "dabench")] == ["b"]
    with pytest.raises(ValueError, match="c"):
        runner.select_cases([*cases, {"id": "c"}], "dabench")


def test_case_meta_leaves_suspect_cases_unscored() -> None:
    case = {"dataset": "dabench", "kind": "data"}
    assert runner.case_meta(case) == {"dataset": "dabench", "kind": "data", "scored": True}
    assert runner.case_meta({**case, "review": {"status": "weak_premise"}})["scored"]
    assert not runner.case_meta({**case, "review": {"status": "suspect"}})["scored"]


KINDS = {"data", "routing", "rule", "behavior"}


def test_every_case_has_a_dataset_a_kind_and_a_unique_id() -> None:
    security = runner.load_security_cases()
    groups = {
        "questions": runner.load_questions(),
        "routing": runner.load_routing_cases(),
        "dangerous_sql": security["dangerous_sql"],
        "prompt_injection": security["prompt_injection"],
        "dabench": runner.load_dabench_cases(),
    }
    ids: list[str] = []
    for name, cases in groups.items():
        for case in cases:
            assert case["dataset"] in runner.DATASET_DATABASES, (name, case["id"])
            assert case["kind"] in KINDS, (name, case["id"])
            ids.append(case["id"])
    assert len(ids) == len(set(ids))
    assert {case["kind"] for case in groups["routing"]} == {"routing"}
    assert {case["kind"] for case in groups["dangerous_sql"]} == {"rule"}
    behavior = {case["category"] for case in groups["questions"] if case["kind"] == "behavior"}
    assert behavior == {"refusal", "direct"}


def test_dabench_cases_carry_their_review() -> None:
    cases = {case["id"]: case for case in runner.load_dabench_cases()}
    assert len(cases) == 10
    assert {case["category"] for case in cases.values()} == {"attribution", "open"}
    assert not runner.case_meta(cases["dab-101"])["scored"]
    assert cases["dab-73"]["review"]["status"] == "weak_premise"
    scored = [case for case in cases.values()
              if case["category"] == "attribution" and runner.case_meta(case)["scored"]]
    assert sorted(case["id"] for case in scored) == [
        "dab-105", "dab-107", "dab-48", "dab-50", "dab-58", "dab-71", "dab-73"]


def test_dabench_question_without_a_review_is_an_error(tmp_path: Path) -> None:
    questions = tmp_path / "q.csv"
    questions.write_text("query_num,query_type,query,root_cause_gt,strict_root_cause_gt,"
                         "train_of_thought,difficulty\n1,归因,问题,,,,中等\n", encoding="utf-8")
    reviews = tmp_path / "r.json"
    reviews.write_text(json.dumps({"reviews": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="1"):
        runner.load_dabench_cases(questions, reviews)


def test_fixed_attribution_is_scored_apart_from_the_pipeline(monkeypatch) -> None:
    from types import SimpleNamespace
    result = SimpleNamespace(summary="ok", factors=[SimpleNamespace(dimension=name) for name in
                                                    ("product", "channel", "carrier")])
    monkeypatch.setattr(runner, "attribute_return_rate", lambda settings, question: result)
    question = {"id": "a01", "category": "attribution", "question": "退货率为什么涨了？",
                "dataset": "ecommerce_demo", "kind": "data"}
    report = runner.evaluate_attribution(None, [question])
    case = report.cases[0]
    assert (case.id, case.kind, case.passed) == ("fa-a01", "fixed_attribution", True)
    assert list(report.as_dict()["groups"]) == ["ecommerce_demo/fixed_attribution"]


class _ReferenceConnection:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.sql: list[str] = []

    def execute(self, statement: Any) -> Any:
        self.sql.append(str(statement))
        rows = self.rows

        class _Result:
            def fetchall(self) -> list[tuple]:
                return rows

        return _Result()


def _reference_settings() -> Any:
    from app.config import Settings
    return Settings(_env_file=None, mysql_eval_password="pw")


def test_reference_answers_use_the_evaluation_account_and_keep_every_row(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager
    from decimal import Decimal
    rows = [(f"商品{index}", Decimal("1.50")) for index in range(600)]
    conn = _ReferenceConnection(rows)
    used: list[Any] = []

    @contextmanager
    def connection(settings: Any, database: str | None = None) -> Any:
        used.append(settings.mysql_eval_user)
        yield conn

    monkeypatch.setattr(runner, "eval_business_connection", connection)
    result = runner.execute_reference(_reference_settings(), "SELECT name, price FROM safe.t;")
    assert used == ["dataagent_eval"]
    assert conn.sql == ["SELECT name, price FROM safe.t"]
    # Past the agent's 500-row limit, with values normalized like the agent's.
    assert len(result) == 600 and result[0] == ["商品0", 1.5]


@pytest.mark.parametrize("sql", ["DELETE FROM safe.t", "SELECT 1; SELECT 2",
                                 "INSERT INTO safe.t VALUES (1)", ""])
def test_reference_answers_must_be_one_query(monkeypatch: pytest.MonkeyPatch, sql: str) -> None:
    monkeypatch.setattr(runner, "eval_business_connection",
                        lambda *args: pytest.fail("must not connect"))
    with pytest.raises(SQLValidationError):
        runner.execute_reference(_reference_settings(), sql)


def test_reference_answers_may_use_tables_the_agent_may_not() -> None:
    from app.safety.validator import check_read_only
    sql = "WITH t AS (SELECT 1 AS a) SELECT a FROM t UNION SELECT 2"
    assert check_read_only(sql + ";", "mysql") == sql


def test_the_evaluation_needs_the_eval_password() -> None:
    from app.config import Settings
    problem = runner.eval_account_problem(Settings(_env_file=None))
    assert "MYSQL_EVAL_PASSWORD" in problem and "grant_eval_user" in problem
    assert runner.eval_account_problem(_reference_settings()) == ""
