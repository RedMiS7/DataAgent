"""Retrieval recall: which tables a question needs and which retrieval points to."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.agent import tools
from app.config import Settings
from app.evaluation import retrieval as retrieval_eval
from app.evaluation.retrieval import (
    expected_tables,
    retrieval_query,
    score_case,
    summarize,
)
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
from app.semantic.table_search import TableChoice, TableSearch

PROJECT_DIR = Path(__file__).resolve().parents[2]

QUESTIONS = json.loads((PROJECT_DIR / "eval" / "questions.json").read_text("utf-8"))
SEED = PROJECT_DIR / "data" / "seed"
ITEMS = [SchemaItem.model_validate(row)
         for row in json.loads((SEED / "schema_items.json").read_text("utf-8"))]
TABLES = [TableSemantic.model_validate(row)
          for row in json.loads((SEED / "semantic_tables.json").read_text("utf-8"))]


def _question(question_id: str) -> dict:
    return next(item for item in QUESTIONS if item["id"] == question_id)


def test_expected_tables_come_from_the_reference_sql() -> None:
    question = {"expected_sql": "SELECT c.name FROM safe.orders o JOIN safe.Channels c "
                                "ON c.id = o.channel_id"}
    assert expected_tables(question, "safe") == ["channels", "orders"]


def test_hand_labelled_tables_count_too() -> None:
    assert expected_tables(_question("a04"), "safe") == [
        "channels", "order_items", "orders", "products", "refunds"]


def test_every_question_that_needs_data_has_tables() -> None:
    without = [item["id"] for item in QUESTIONS
               if item["category"] not in ("refusal", "direct")
               and not expected_tables(item, "safe")]
    assert without == []
    # Only the known tables are labelled.
    known = {row.table_name.removeprefix("safe.") for row in TABLES}
    labelled = {table for item in QUESTIONS for table in item.get("expected_tables", [])}
    assert labelled <= known


def test_a_followup_is_retrieved_with_the_earlier_question() -> None:
    assert retrieval_query(_question("f01")) == "上个月GMV是多少？；追问：那上上个月呢？"
    assert retrieval_query(_question("c01")).startswith("上个月按退款申请时间算")
    # Not a follow-up: the earlier question stays out, as in the pipeline.
    assert retrieval_query(_question("t04")) == "上个月的订单数是多少？"
    assert retrieval_query(_question("q01")) == "最近30天有多少订单？"


def test_score_and_summary() -> None:
    tokens = {"orders": 400, "order_items": 200, "refunds": 300}
    found_all = score_case({"id": "1", "category": "metric", "question": "q"}, "q",
                           ["order_items", "orders"], ["orders", "order_items", "refunds"], tokens)
    missed = score_case({"id": "2", "category": "metric", "question": "q"}, "q",
                        ["orders", "shipments"], ["orders"], tokens)
    assert found_all.passed and found_all.tokens == 900
    assert missed.missing == ["shipments"] and not missed.passed
    summary = summarize([found_all, missed])
    assert (summary["passed"], summary["total"], summary["recall"]) == (1, 2, 0.5)
    assert summary["table_recall"] == 0.75
    assert summary["tables_avg"] == 2.0 and summary["tokens_avg"] == 650
    assert summary["cases"][1]["missing"] == ["shipments"]


def test_retrieved_tables_follow_columns_metrics_then_cases() -> None:
    item = next(item for item in ITEMS if item.table_name == "safe.shipments")
    bundle = RetrievalBundle(
        schema_items=[SchemaHit(item=item)],
        metrics=[MetricHit(metric=Metric(name="GMV", definition="", formula="",
                                         related_tables="safe.orders, safe.order_items"))],
        cases=[CaseHit(id=1, question="", sql_text="SELECT 1 FROM safe.refunds r "
                                                   "JOIN safe.orders o ON o.id = r.order_id")],
    )
    assert tools.retrieved_tables(bundle, "safe") == ["shipments", "orders", "order_items",
                                                      "refunds"]


class _Connection:
    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class _Retriever:
    """Points to the orders table whatever the question."""

    def retrieve_with_connection(self, question: str, conn: Any) -> RetrievalBundle:
        item = next(item for item in ITEMS if item.table_name == "safe.orders")
        return RetrievalBundle(schema_items=[SchemaHit(item=item)])


class _Searcher:
    """Finds orders and categories, as if completion had joined them."""

    def __init__(self, *args: Any) -> None:
        return None

    def search(self, query: str, conn: Any, *, rerank: bool, complete: bool) -> TableSearch:
        tables = [TableChoice("orders", ["检索命中"])]
        if complete:
            tables.append(TableChoice("categories", ["关联补全"]))
        return TableSearch(query=query, tables=tables, candidates=["orders"], metrics=[],
                           cases=[], entities=[], reranked=rerank,
                           rerank_note="")


def test_evaluate_retrieval_scores_every_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval_eval, "eval_app_connection", lambda settings: _Connection())
    monkeypatch.setattr(retrieval_eval, "SemanticRetriever", lambda *args: _Retriever())
    monkeypatch.setattr(retrieval_eval, "TableSearcher", _Searcher)
    monkeypatch.setattr(tools, "table_columns",
                        lambda settings: {"orders": [("id", "bigint")],
                                          "categories": [("id", "int")]})
    monkeypatch.setattr(repository, "fetch_schema_items", lambda conn, enabled_only: ITEMS)
    monkeypatch.setattr(repository, "fetch_tables", lambda conn: TABLES)
    questions = [
        {"id": "x1", "category": "single_table", "question": "订单数",
         "expected_sql": "SELECT COUNT(*) FROM safe.orders"},
        {"id": "x2", "category": "multi_table", "question": "各类目",
         "expected_sql": "SELECT 1 FROM safe.orders o JOIN safe.categories c ON 1 = 1"},
        # Needs no table: left out.
        {"id": "x3", "category": "direct", "question": "你好", "expected_sql": ""},
    ]
    report = retrieval_eval.evaluate_retrieval(Settings(_env_file=None), _Retriever(),  # type: ignore[arg-type]
                                               questions)
    assert set(report["variants"]) == {*retrieval_eval.VARIANTS, *retrieval_eval.SEARCH_VARIANTS}
    plain = report["variants"]["vector"]
    assert (plain["passed"], plain["total"]) == (1, 2)
    assert plain["cases"][1]["missing"] == ["categories"]
    assert plain["tokens_avg"] > 0
    assert report["vector_degraded"] == []
    searched = report["variants"]["hybrid+rerank+complete"]
    assert (searched["passed"], searched["total"]) == (2, 2)
    assert searched["cases"][1]["reasons"] == {"orders": ["检索命中"], "categories": ["关联补全"]}
    assert report["variants"]["hybrid"]["passed"] == 1
