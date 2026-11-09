"""Retrieval recall: does retrieval point to every table a question needs?

No chat model is called, only the embedding and the rerank model: each question's
retrieval query goes through retrieval as the pipeline sends it, and the tables
retrieval points to are compared with the tables the question needs.

The first four variants are the retrieval the pipeline uses for its prompt; the rest are
the table search (hybrid retrieval, rerank, completion) with parts of it turned off.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from app.agent import tools
from app.agent.pipeline import build_retrieval_query
from app.config import Settings
from app.db.engine import eval_app_connection
from app.rerank import Reranker
from app.semantic import repository
from app.semantic.retriever import SemanticRetriever
from app.semantic.schema_context import (
    bare_name,
    connect,
    estimate_tokens,
    join_graph,
    render_table,
)
from app.semantic.table_search import TableSearcher

# How the found tables are turned into what the model gets, by variant name.
VARIANTS: dict[str, tuple[str, bool]] = {
    "vector": ("vector", False),
    "vector+connect": ("vector", True),
    "keyword": ("keyword", False),
    "keyword+connect": ("keyword", True),
}
# Table search variants: (rerank, complete).
SEARCH_VARIANTS: dict[str, tuple[bool, bool]] = {
    "hybrid": (False, False),
    "hybrid+complete": (False, True),
    "hybrid+rerank": (True, False),
    "hybrid+rerank+complete": (True, True),
}
VARIANT_LABELS = {
    "vector": "向量检索",
    "vector+connect": "向量检索 + 关联补全",
    "keyword": "关键词检索",
    "keyword+connect": "关键词检索 + 关联补全",
    "hybrid": "混合检索",
    "hybrid+complete": "混合检索 + 补全",
    "hybrid+rerank": "混合检索 + 重排",
    "hybrid+rerank+complete": "混合检索 + 重排 + 补全",
}


@dataclass(slots=True)
class RetrievalCase:
    id: str
    category: str
    question: str
    # The text retrieval gets: a follow-up carries the earlier question.
    query: str
    expected: list[str]
    found: list[str]
    missing: list[str]
    # Estimated tokens of the found tables, written out with all of their columns.
    tokens: int
    # Milliseconds the search took; 0 where one retrieval served several variants.
    ms: int = 0
    # Why each table was taken, for the table search variants.
    reasons: dict[str, list[str]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.missing

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "passed": self.passed}


def expected_tables(question: dict, schema: str) -> list[str]:
    """The tables a question needs: those its reference SQL reads, and any labelled by
    hand (``expected_tables``) for questions without one."""
    tables = set(tools.table_names(question.get("expected_sql") or "", schema))
    tables.update(name.lower() for name in question.get("expected_tables", []))
    return sorted(tables)


def earlier_questions(question: dict) -> list[str]:
    """The user's earlier questions of a case, oldest first."""
    if question.get("history_messages"):
        return [item["content"] for item in question["history_messages"]
                if item["role"] == "user"]
    return list(question.get("turns") or question.get("history") or [])


def retrieval_query(question: dict) -> str:
    """What the pipeline would send to retrieval for this case."""
    # Retrieval only reads the earlier user questions, so no replies are needed.
    turns = [("user", text) for text in earlier_questions(question)]
    return build_retrieval_query(question["question"], turns)


def score_case(question: dict, query: str, expected: list[str], found: list[str],
               table_tokens: Mapping[str, int], ms: int = 0,
               reasons: dict[str, list[str]] | None = None) -> RetrievalCase:
    return RetrievalCase(
        id=question["id"], category=question["category"], question=question["question"],
        query=query, expected=expected, found=found,
        missing=[table for table in expected if table not in found],
        tokens=sum(table_tokens.get(table, 0) for table in found), ms=ms,
        reasons=reasons or {},
    )


def summarize(cases: list[RetrievalCase]) -> dict[str, Any]:
    total = len(cases)
    expected = sum(len(case.expected) for case in cases)
    found = sum(len(case.expected) - len(case.missing) for case in cases)
    return {
        "total": total,
        # Questions whose tables were all found.
        "passed": sum(case.passed for case in cases),
        "recall": round(sum(case.passed for case in cases) / total, 4) if total else None,
        # Needed tables that were found, over all questions.
        "table_recall": round(found / expected, 4) if expected else None,
        "tables_avg": round(sum(len(case.found) for case in cases) / total, 1) if total else 0.0,
        "tokens_avg": round(sum(case.tokens for case in cases) / total) if total else 0,
        "ms_avg": round(sum(case.ms for case in cases) / total) if total else 0,
        "cases": [case.as_dict() for case in cases],
    }


def evaluate_retrieval(settings: Settings, retriever: SemanticRetriever,
                       questions: list[dict], reranker: Reranker | None = None
                       ) -> dict[str, Any]:
    """Recall of each variant over the questions that need tables."""
    schema = settings.mysql_business_db
    prefix = f"{schema}."
    columns = tools.table_columns(settings) or {}
    retrievers: dict[str, SemanticRetriever] = {
        "vector": retriever,
        # Without a vector store, retrieval matches keywords only.
        "keyword": SemanticRetriever(settings, None, None),
    }
    searcher = TableSearcher(settings, retriever, reranker)
    results: dict[str, list[RetrievalCase]] = {name: [] for name in [*VARIANTS, *SEARCH_VARIANTS]}
    degraded: set[str] = set()
    rerank_notes: set[str] = set()
    # Reads the semantic layer with the evaluation account, which may not write.
    with eval_app_connection(settings) as conn:
        items = repository.fetch_schema_items(conn, enabled_only=False)
        graph = join_graph(items, prefix)
        by_key = {(item.table_name, item.column_name): item for item in items}
        summaries = {bare_name(table.table_name, prefix): table.summary
                     for table in repository.fetch_tables(conn)}
        table_tokens = {table: estimate_tokens(render_table(table, table_columns, by_key,
                                                            prefix, summaries.get(table, "")))
                        for table, table_columns in columns.items()}
        for question in questions:
            expected = expected_tables(question, schema)
            if not expected:
                continue
            query = retrieval_query(question)
            found_by: dict[str, list[str]] = {}
            for name, source in retrievers.items():
                bundle = source.retrieve_with_connection(query, conn)
                if name == "vector" and bundle.degraded:
                    degraded.add(bundle.degraded_reason)
                found_by[name] = tools.retrieved_tables(bundle, schema)
            for variant, (source_name, with_connect) in VARIANTS.items():
                found = found_by[source_name]
                if with_connect:
                    found = connect(found, graph)
                results[variant].append(score_case(question, query, expected, found,
                                                   table_tokens))
            for variant, (rerank, complete) in SEARCH_VARIANTS.items():
                started = time.perf_counter()
                search = searcher.search(query, conn, rerank=rerank, complete=complete)
                ms = int((time.perf_counter() - started) * 1000)
                if search.degraded:
                    degraded.add(search.degraded)
                if search.rerank_note:
                    rerank_notes.add(search.rerank_note)
                results[variant].append(score_case(
                    question, query, expected, search.table_names, table_tokens, ms,
                    {choice.table: choice.reasons for choice in search.tables}))
    return {
        "variants": {name: summarize(cases) for name, cases in results.items()},
        # Set when the vector variant fell back to keywords, so it measured nothing new.
        "vector_degraded": sorted(degraded),
        # Set when the rerank variants could not rerank and kept the retrieval order.
        "rerank_notes": sorted(rerank_notes),
        "rerank_model": reranker.model_name if reranker else "",
    }


def print_retrieval(report: dict[str, Any]) -> None:
    print("\n=== 检索召回率 ===")
    for reason in report["vector_degraded"]:
        print(f"  注意：向量检索不可用，向量的结果其实是关键词检索：{reason}")
    for note in report.get("rerank_notes", []):
        print(f"  注意：重排没有生效，重排的结果其实是检索排序：{note}")
    if report.get("rerank_model"):
        print(f"  重排模型：{report['rerank_model']}")
    for name, summary in report["variants"].items():
        recall = (summary["recall"] or 0) * 100
        table_recall = (summary["table_recall"] or 0) * 100
        print(f"  {VARIANT_LABELS[name]}：找全 {summary['passed']}/{summary['total']}"
              f"（{recall:.1f}%），表召回率 {table_recall:.1f}%，"
              f"平均 {summary['tables_avg']} 张表、约 {summary['tokens_avg']} token"
              + (f"、{summary['ms_avg']} ms" if summary.get("ms_avg") else ""))
        missed = [case for case in summary["cases"] if not case["passed"]]
        for case in missed[:30]:
            print(f"    - {case['id']} [{case['category']}] {case['question'][:30]}"
                  f" 漏了 {'、'.join(case['missing'])}（找到 {'、'.join(case['found']) or '无'}）")
