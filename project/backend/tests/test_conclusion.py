"""Conclusions with evidence (plan 2.4): reading, checking, rewriting once, and showing."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from agentscope.message import TextBlock
from app.agent import pipeline
from app.agent.conclusion import (
    EVIDENCE_ROWS,
    archived,
    cited_queries,
    queries_digest,
    read_conclusion,
    render_summary,
)
from app.agent.scope_model import ScriptedChatModel
from app.evaluation import report
from app.evaluation.runner import CaseResult, conclusion_metrics, conclusion_state
from app.nl2sql.executor import ExecutionResult
from test_pipeline import (  # noqa: F401  (the fixtures are used by name)
    AUGUST_SQL,
    CATEGORY_SQL,
    NO_PLAN,
    OVERALL_SQL,
    SEPTEMBER_SQL,
    analyze,
    ask,
    call,
)

RESULTS = [
    ExecutionResult(columns=["期间", "退货率"], rows=[["本期", 4.3], ["上期", 3.9]], sql="a"),
    ExecutionResult(columns=["渠道", "增量"], rows=[[f"渠道{n}", n] for n in range(12)], sql="b"),
]


def conclusion(**fields: Any) -> str:
    data = {"judgement": "退货率上升了 0.4 个百分点",
            "points": [{"claim": "本期 4.3%，上期 3.9%",
                        "evidence": [{"query": 1, "rows": [0, 1], "columns": ["期间", "退货率"]}]},
                       {"claim": "渠道11 增量最大",
                        "evidence": [{"query": 2, "rows": [11], "columns": ["渠道", "增量"]}]}],
            "advice": "先查渠道11"}
    data.update(fields)
    return json.dumps(data, ensure_ascii=False)


# Reading and checking

def test_a_conclusion_with_right_evidence_is_read() -> None:
    reading = read_conclusion(conclusion(), RESULTS, "analysis")
    assert reading.ok
    read = reading.conclusion
    assert read is not None and read.checked
    assert cited_queries(read) == [1, 2]
    # The evidence table is cut from the query result, not written by the model.
    first = read.points[0].evidence[0].table
    assert first is not None and first.rows == [["本期", 4.3], ["上期", 3.9]]
    assert read.points[1].evidence[0].table.rows == [["渠道11", 11]]  # type: ignore[union-attr]
    assert render_summary(read, "analysis") == (
        "退货率上升了 0.4 个百分点\n- 本期 4.3%，上期 3.9%[查询1]\n"
        "- 渠道11 增量最大[查询2]\n- 建议：先查渠道11")


def test_text_around_the_json_is_tolerated() -> None:
    assert read_conclusion(f"```json\n{conclusion()}\n```", RESULTS, "analysis").ok
    assert read_conclusion(f"结论如下：{conclusion()}", RESULTS, "analysis").ok


@pytest.mark.parametrize("evidence, error", [
    ({"query": 3, "rows": [0]}, "没有查询 3，只有查询 1 到 2"),
    ({"query": 0}, "没有查询 0"),
    ({"query": 1, "rows": [2]}, "查询 1 没有第 2 行（行号从 0 开始，共 2 行）"),
    ({"query": 1, "columns": ["比率"]}, "查询 1 没有列 比率（有：期间、退货率）"),
    ({"query": "一"}, "query 要写查询编号"),
    ({"query": 1, "rows": "第一行"}, "rows 要写行号列表"),
    ("查询1", "依据要写成"),
])
def test_wrong_evidence_is_reported_and_left_out(evidence: Any, error: str) -> None:
    reading = read_conclusion(conclusion(points=[
        {"claim": "说法", "evidence": [evidence, {"query": 1, "rows": [0]}]}]), RESULTS, "analysis")
    assert not reading.ok
    assert any(error in item for item in reading.errors), reading.errors
    read = reading.conclusion
    assert read is not None and not read.checked
    # The right evidence of the same point stays.
    assert [(item.query, item.rows) for item in read.points[0].evidence] == [(1, [0])]


def test_a_point_without_evidence_is_an_error() -> None:
    reading = read_conclusion(conclusion(points=[{"claim": "说法"}]), RESULTS, "analysis")
    assert reading.errors == ["第 1 条要点：没有依据，每条要点至少要有一条依据"]


@pytest.mark.parametrize("text, error", [
    ("退货率上升了。", "结论不是 JSON"),
    ('{"judgement": "上升", "points": [', "JSON 格式错误"),
])
def test_text_that_is_no_conclusion_has_no_structure(text: str, error: str) -> None:
    reading = read_conclusion(text, RESULTS, "analysis")
    assert reading.conclusion is None
    assert error in reading.errors[0]


def test_missing_parts_are_errors() -> None:
    reading = read_conclusion(conclusion(judgement="", points=[]), RESULTS, "analysis")
    assert reading.conclusion is None
    assert reading.errors == ["缺少 judgement（一句判断）", "缺少 points（要点和依据）"]


def test_empty_rows_and_columns_mean_the_whole_table_up_to_a_limit() -> None:
    read = read_conclusion(conclusion(points=[{"claim": "各渠道", "evidence": {"query": 2}}]),
                           RESULTS, "analysis").conclusion
    assert read is not None and read.checked
    table = read.points[0].evidence[0].table
    assert table is not None and table.columns == ["渠道", "增量"]
    assert len(table.rows) == EVIDENCE_ROWS and table.truncated and table.row_count == 12


def test_lenient_forms_of_a_citation_are_read() -> None:
    read = read_conclusion(conclusion(points=[
        {"claim": "说法", "evidence": [{"query": "查询1", "rows": 1, "columns": "退货率"}]}]),
        RESULTS, "analysis").conclusion
    assert read is not None and read.checked
    evidence = read.points[0].evidence[0]
    assert (evidence.query, evidence.rows, evidence.columns) == (1, [1], ["退货率"])


def test_a_query_answer_is_its_judgement_without_advice() -> None:
    read = read_conclusion(conclusion(), RESULTS, "query").conclusion
    assert read is not None and read.advice == ""
    assert render_summary(read, "query") == "退货率上升了 0.4 个百分点"


def test_the_archive_leaves_out_the_cells() -> None:
    read = read_conclusion(conclusion(), RESULTS, "analysis").conclusion
    kept = archived(read)
    assert kept is not None and "table" not in kept["points"][0]["evidence"][0]
    assert kept["points"][0]["evidence"][0]["rows"] == [0, 1]
    assert archived(None) is None


def test_the_digest_numbers_queries_and_rows() -> None:
    digest = queries_digest(RESULTS[:1], ["两期对比"])
    assert digest == "[查询1] 两期对比\n行号 | 期间 | 退货率\n0 | 本期 | 4.3\n1 | 上期 | 3.9"


# In the pipeline: the fixtures of test_pipeline, under names of their own

@pytest.fixture
def run_query(request: pytest.FixtureRequest) -> Any:
    return request.getfixturevalue("ask")


@pytest.fixture
def run_analysis(request: pytest.FixtureRequest) -> Any:
    return request.getfixturevalue("analyze")


class RecordingModel(ScriptedChatModel):
    def __init__(self, script: list[list[Any]]) -> None:
        super().__init__(script)
        self.seen: list[str] = []

    async def _call_api(self, model: str, messages: list | None = None, *args: Any,
                        **kwargs: Any) -> Any:
        self.seen.append(json.dumps(messages, ensure_ascii=False, default=str))
        return await super()._call_api(model, messages, *args, **kwargs)


def query_conclusion(query: int, rows: list[int] | None = None, **fields: Any) -> TextBlock:
    data = {"judgement": "上上个月销量最差的是母婴热销款145号，63件。",
            "points": [{"claim": "母婴热销款145号 63 件",
                        "evidence": [{"query": query, "rows": [0] if rows is None else rows,
                                      "columns": ["name", "qty"]}]}]}
    data.update(fields)
    return TextBlock(text=json.dumps(data, ensure_ascii=False))


def rewrite_with(monkeypatch: pytest.MonkeyPatch, *replies: str) -> RecordingModel:
    model = RecordingModel([[TextBlock(text=reply)] for reply in replies])
    monkeypatch.setattr(pipeline, "build_rewrite_model", lambda settings: model)
    return model


def test_a_query_answer_shows_the_table_its_evidence_cites(run_query) -> None:
    # The model ran one more query after the answer; the evidence says which one it used.
    answer = run_query("上上个月哪个商品卖的最差", [
        [call("run_sql", sql=AUGUST_SQL)],
        [call("run_sql", sql=SEPTEMBER_SQL)],
        [query_conclusion(1)],
    ])
    assert answer.status == "ok"
    assert answer.table is not None and answer.table.rows == [["母婴热销款145号", 63]]
    assert answer.summary == "上上个月销量最差的是母婴热销款145号，63件。"
    assert answer.conclusion is not None and answer.conclusion.checked
    evidence = answer.conclusion.points[0].evidence[0]
    assert evidence.table is not None and evidence.table.rows == answer.table.rows
    # The archive keeps the citation, not the cells.
    kept = run_query.saved["log"]["conclusion"]
    assert kept["points"][0]["evidence"][0] == {"query": 1, "rows": [0], "columns": ["name", "qty"]}


def test_wrong_evidence_is_rewritten_once(run_query, monkeypatch: pytest.MonkeyPatch) -> None:
    rewrite = rewrite_with(monkeypatch, query_conclusion(1).text)
    answer = run_query("上上个月哪个商品卖的最差", [
        [call("run_sql", sql=AUGUST_SQL)],
        [query_conclusion(5)],
    ])
    assert answer.conclusion is not None and answer.conclusion.checked
    assert answer.summary == "上上个月销量最差的是母婴热销款145号，63件。"
    # The rewrite reads what was wrong and every query result, with row numbers.
    assert rewrite.calls == 1
    assert "没有查询 5，只有查询 1 到 1" in rewrite.seen[0]
    assert "0 | 母婴热销款145号 | 63" in rewrite.seen[0]
    assert answer.usage is not None and answer.usage.model_calls == 4


def test_evidence_still_wrong_after_the_rewrite_is_left_out(
        run_query, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    rewrite_with(monkeypatch, query_conclusion(1, rows=[9]).text)
    with caplog.at_level(logging.WARNING, logger="app"):
        answer = run_query("上上个月哪个商品卖的最差", [
            [call("run_sql", sql=AUGUST_SQL)],
            [call("run_sql", sql=SEPTEMBER_SQL)],
            [query_conclusion(7)],
        ])
    assert answer.conclusion is not None and not answer.conclusion.checked
    assert answer.conclusion.points[0].evidence == []
    assert answer.summary == "上上个月销量最差的是母婴热销款145号，63件。"
    # Nothing checked says which table: the last one with rows is shown.
    assert answer.table is not None and answer.table.rows == [["童装热销款208号", 176]]
    assert any("重写后仍有问题" in item.getMessage() for item in caplog.records)


def test_text_that_is_never_json_is_shown_as_it_is(run_query, monkeypatch: pytest.MonkeyPatch) -> None:
    rewrite_with(monkeypatch, "还是一句话")
    answer = run_query("上上个月哪个商品卖的最差", [
        [call("run_sql", sql=AUGUST_SQL)],
        [TextBlock(text="销量最差的是母婴热销款145号，仅63件。")],
    ])
    assert answer.conclusion is None
    assert answer.summary == "销量最差的是母婴热销款145号，仅63件。"
    assert answer.summary_source == "model"


def test_a_rewrite_that_reads_is_kept_over_text(run_query, monkeypatch: pytest.MonkeyPatch) -> None:
    rewrite_with(monkeypatch, query_conclusion(1).text)
    answer = run_query("上上个月哪个商品卖的最差", [
        [call("run_sql", sql=AUGUST_SQL)],
        [TextBlock(text="销量最差的是母婴热销款145号，仅63件。")],
    ])
    assert answer.conclusion is not None and answer.conclusion.checked


def test_an_analysis_with_wrong_evidence_shows_every_query(run_analysis, monkeypatch) -> None:
    rewrite_with(monkeypatch, "")
    wrong = json.dumps({"judgement": "整体不错。", "points": [
        {"claim": "GMV 966.6 万", "evidence": [{"query": 1, "rows": [0]}]},
        {"claim": "鞋靴 200", "evidence": [{"query": 9}]}]}, ensure_ascii=False)
    answer, saved = run_analysis([
        [call("run_sql", sql=OVERALL_SQL, purpose="整体")],
        [call("run_sql", sql=CATEGORY_SQL, purpose="类目")],
        [TextBlock(text=wrong)],
    ])
    assert answer.conclusion is not None and not answer.conclusion.checked
    assert [item.index for item in answer.results] == [1, 2]
    assert answer.summary == "整体不错。\n- GMV 966.6 万[查询1]\n- 鞋靴 200"
    assert any(data.get("step") == "rewriting" for event, data in saved["events"])


def test_an_answer_without_a_query_needs_no_conclusion(run_query) -> None:
    answer = run_query("GMV 的口径是什么？", [[TextBlock(text="GMV 是支付成功订单的商品金额。")]])
    assert answer.status == "text"
    assert answer.conclusion is None


# Evaluation

def test_the_evaluation_reads_how_the_conclusion_was_read(run_query, monkeypatch) -> None:
    good = run_query("上上个月哪个商品卖的最差", [[call("run_sql", sql=AUGUST_SQL)], [query_conclusion(1)]])
    assert conclusion_state(good) == "ok"
    rewrite_with(monkeypatch, "")
    raw = run_query("上上个月哪个商品卖的最差", [[call("run_sql", sql=AUGUST_SQL)],
                                     [TextBlock(text="一句话")]])
    assert conclusion_state(raw) == "raw"
    text = run_query("GMV 的口径是什么？", [[TextBlock(text="口径。")]])
    assert conclusion_state(text) == ""


def test_conclusion_figures_are_counted_and_printed(capsys: pytest.CaptureFixture[str]) -> None:
    def case(state: str, points: int = 0, with_evidence: int = 0, rewrites: int = 0) -> CaseResult:
        return CaseResult(id="x", category="analysis", question="q", passed=True,
                          conclusion_state=state, points=points,
                          points_with_evidence=with_evidence, rewrite_calls=rewrites)

    figures = conclusion_metrics([case("ok", 3, 3), case("unchecked", 2, 1, 1), case("raw", rewrites=1),
                                  case("")])
    assert figures == {"answers": 3, "ok": 1, "unchecked": 1, "raw": 1, "rewritten": 2,
                       "points": 5, "points_with_evidence": 4}
    report.print_conclusion(figures)
    printed = capsys.readouterr().out
    assert "依据都对 1/3（33%）" in printed and "有依据 4 条" in printed
