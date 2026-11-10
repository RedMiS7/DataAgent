"""Find the tables a question needs: hybrid retrieval, per-table ranking, rerank, completion.

1. Vector and keyword retrieval over columns, tables and cases. Each list is turned
   into tables in order of first appearance, and the lists are merged by rank (RRF).
2. The best ``rerank_candidates`` tables go to the rerank model, which keeps
   ``search_top_tables`` of them. Without a rerank model the merged order is kept.
3. Completion adds what the chosen tables cannot do without: the tables their matched
   columns link to, the tables of the matched metrics, the tables of the values named
   in the question, and the tables needed to join them all.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy.engine import Connection

from ..agent.trace import CallTrace, timed
from ..config import Settings
from ..db.engine import business_connection
from ..embedding.base import EmbeddingError
from ..rerank import Reranker, RerankError
from ..vector.base import VectorStoreError
from . import repository
from .models import CaseHit, EntityHit, Metric, MetricHit, RetrievalBundle, SchemaItem, TableSemantic
from .retriever import SemanticRetriever, _keyword_rank
from .schema_context import (
    TableColumns,
    bare_name,
    connect,
    estimate_tokens,
    hint_keys,
    join_graph,
    pointed_tables,
    render_table,
    table_names,
)

logger = logging.getLogger(__name__)

# The usual constant of reciprocal rank fusion: ranks far down a list still count a little.
RRF_K = 60
# Hits read from each list before fusion.
LIST_DEPTH = 30
# Metrics whose tables completion adds.
METRIC_TABLES = 2
# Entity values a search returns.
ENTITY_LIMIT = 5

RETRIEVED = "检索命中"
RERANKED = "重排选中"
POINTED = "外键指向"
METRIC = "口径需要"
ENTITY = "实体所在"
JOINED = "关联补全"


@dataclass(slots=True)
class TableChoice:
    table: str
    reasons: list[str] = field(default_factory=list)
    # The rerank score, or the fused retrieval score when the table was not reranked;
    # 0 for tables completion added.
    score: float = 0.0


@dataclass(slots=True)
class TableSearch:
    query: str
    # Chosen tables first, then the ones completion added.
    tables: list[TableChoice]
    # Tables that went to the rerank model, best first by retrieval.
    candidates: list[str]
    metrics: list[MetricHit]
    cases: list[CaseHit]
    entities: list[EntityHit]
    reranked: bool = False
    # Why rerank did not run or failed; empty when it ran or was not asked for.
    rerank_note: str = ""
    # Set when vector retrieval was unavailable and keywords alone were used.
    degraded: str = ""

    @property
    def table_names(self) -> list[str]:
        return [choice.table for choice in self.tables]


def fuse(ranked_lists: Iterable[Sequence[str]]) -> list[tuple[str, float]]:
    """Reciprocal rank fusion: a key scores 1 / (RRF_K + rank) in every list it is in."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, key in enumerate(dict.fromkeys(ranked), start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def first_tables(columns: Iterable[str]) -> list[str]:
    """Tables in order of their first column in ``columns`` ("orders.paid_at")."""
    return list(dict.fromkeys(column.split(".")[0] for column in columns))


def table_document(table: str, summary: str, items: Sequence[SchemaItem], prefix: str) -> str:
    """How a table is shown to the rerank model: name, summary and columns."""
    columns = "；".join(f"{item.column_name} {item.description}" for item in items
                       if bare_name(item.table_name, prefix) == table)
    return f"{prefix}{table}：{summary}。字段：{columns}"


def _add(choices: list[TableChoice], tables: Iterable[str], reason: str,
         known: set[str]) -> None:
    """Add each table not chosen yet; a table already chosen keeps its reason."""
    present = {choice.table for choice in choices}
    for table in tables:
        if table in known and table not in present:
            choices.append(TableChoice(table, [reason]))
            present.add(table)


@dataclass(slots=True)
class _Lists:
    """Ranked hits of one retrieval method."""

    columns: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    cases: list[str] = field(default_factory=list)


class TableSearcher:
    def __init__(self, settings: Settings, retriever: SemanticRetriever,
                 reranker: Reranker | None, trace: CallTrace | None = None) -> None:
        self.settings = settings
        self.retriever = retriever
        self.reranker = reranker
        # The question's trace: embedding and rerank calls are recorded on it.
        self.trace = trace

    def search(self, query: str, conn: Connection, *, use_vector: bool = True,
               use_keyword: bool = True, rerank: bool = True,
               complete: bool = True) -> TableSearch:
        settings = self.settings
        schema = settings.mysql_business_db
        prefix = f"{schema}."
        tables = repository.fetch_tables(conn)
        items = repository.fetch_schema_items(conn)
        all_items = repository.fetch_schema_items(conn, enabled_only=False)
        metrics = repository.fetch_metrics(conn)
        cases = repository.fetch_cases(conn)
        with business_connection(settings) as business_conn:
            entities = repository.fetch_entity_values(business_conn, all_items)
            keys = repository.fetch_foreign_keys(business_conn)
        known = {bare_name(table.table_name, prefix) for table in tables}

        methods: list[_Lists] = []
        vector: list[float] | None = None
        degraded = ""
        if use_vector:
            try:
                vector, lists = self._vector_lists(query, prefix)
                methods.append(lists)
            except (VectorStoreError, RuntimeError) as exc:
                degraded = f"向量检索不可用：{exc}"
                logger.warning("表检索只用关键词：%s", degraded)
                if self.trace is not None and not self.retriever._uses_keyword_only():
                    service = "embedding" if isinstance(exc, EmbeddingError) else "retrieval"
                    self.trace.degrade(service, "向量检索不可用，已使用关键词检索。")
        if use_keyword or degraded:
            methods.append(self._keyword_lists(query, tables, items, metrics, cases, prefix))

        case_by_id = {str(case.id): case for case in cases}
        fused_cases = [case_by_id[key] for key, _ in fuse(lists.cases for lists in methods)
                       if key in case_by_id]
        table_lists = [first_tables(lists.columns) for lists in methods]
        table_lists += [lists.tables for lists in methods]
        table_lists.append([table for case in fused_cases
                            for table in table_names(case.sql_text, schema)])
        fused_tables = [(table, score) for table, score in fuse(table_lists) if table in known]
        candidates = [table for table, _ in fused_tables[:settings.rerank_candidates]]

        chosen, reranked, note = self._choose(query, dict(fused_tables), candidates, tables,
                                              all_items, prefix, rerank)
        choices = [TableChoice(table, [RERANKED if reranked else RETRIEVED], score)
                   for table, score in chosen]

        metric_by_name = {metric.name: metric for metric in metrics}
        fused_metrics = [MetricHit(metric=metric_by_name[name], score=score, source="hybrid")
                         for name, score in fuse(lists.metrics for lists in methods)
                         if name in metric_by_name]
        matched_entities = self._entities(query, vector, entities)

        if complete:
            all_keys = [*keys, *hint_keys(all_items, prefix)]
            chosen_tables = {choice.table for choice in choices}
            top_columns = [key for key, _ in fuse(lists.columns for lists in methods)]
            linked_columns = [tuple(column.split(".", 1))
                              for column in top_columns[:settings.retrieval_top_k]]
            _add(choices, pointed_tables([(table, column) for table, column in linked_columns
                                          if table in chosen_tables], all_keys),
                 POINTED, known)
            for hit in fused_metrics[:METRIC_TABLES]:
                _add(choices, [bare_name(part, prefix) for part in
                               hit.metric.related_tables.split(",") if part.strip()],
                     METRIC, known)
            # Only values the question names word for word: close values found by vectors
            # matched some product or place in almost every question.
            _add(choices, [bare_name(entity.table_name, prefix) for entity in matched_entities
                           if entity.table_name and entity.source == "exact"], ENTITY, known)
            graph = join_graph(all_items, prefix, keys)
            _add(choices, connect([choice.table for choice in choices], graph), JOINED, known)

        return TableSearch(
            query=query, tables=choices, candidates=candidates,
            metrics=fused_metrics[:settings.retrieval_top_k],
            cases=fused_cases[:settings.retrieval_top_k], entities=matched_entities,
            reranked=reranked, rerank_note=note, degraded=degraded,
        )

    def _vector_lists(self, query: str, prefix: str) -> tuple[list[float], _Lists]:
        store, embedder = self.retriever.store, self.retriever.embedder
        if store is None or embedder is None or embedder.model_name.startswith("fake"):
            raise RuntimeError("没有配置向量库或向量模型")
        with timed(self.trace, "embedding", embedder.model_name, input_text=query):
            vector = embedder.embed([query])[0]
        threshold = self.settings.retrieval_min_score

        def payloads(collection: str) -> list[dict]:
            hits = store.search(self.settings.collection_name(collection), vector, LIST_DEPTH)
            return [hit.payload for hit in hits if hit.score >= threshold]

        return vector, _Lists(
            columns=[f"{bare_name(str(payload['table']), prefix)}.{payload['column']}"
                     for payload in payloads("schema_items")],
            metrics=[str(payload["name"]) for payload in payloads("metrics")],
            cases=[str(payload["case_id"]) for payload in payloads("qa_cases")],
        )

    @staticmethod
    def _keyword_lists(query: str, tables: list[TableSemantic], items: list[SchemaItem],
                       metrics: list[Metric], cases: list[CaseHit], prefix: str) -> _Lists:
        def ranked(pairs: list[tuple[object, str]]) -> list:
            return [item for item, _ in _keyword_rank(query, pairs, LIST_DEPTH)]

        column_hits: list[SchemaItem] = ranked([
            (item, f"{item.table_name}.{item.column_name} {item.description} {item.synonyms} "
                   f"{item.join_hint}") for item in items])
        table_hits: list[TableSemantic] = ranked(
            [(table, f"{table.table_name} {table.summary}") for table in tables])
        metric_hits: list[Metric] = ranked(
            [(metric, f"{metric.name} {metric.aliases} {metric.definition}") for metric in metrics])
        case_hits: list[CaseHit] = ranked([(case, case.question) for case in cases])
        return _Lists(
            columns=[f"{bare_name(item.table_name, prefix)}.{item.column_name}"
                     for item in column_hits],
            tables=[bare_name(table.table_name, prefix) for table in table_hits],
            metrics=[metric.name for metric in metric_hits],
            cases=[str(case.id) for case in case_hits],
        )

    def _entities(self, query: str, vector: list[float] | None,
                  entities: list[dict[str, str]]) -> list[EntityHit]:
        """Values named in the question; with a vector, close values too."""
        if vector is not None:
            return self.retriever._match_entities(query, vector, entities)
        hits = [EntityHit(entity_type=row["entity_type"], value=row["value"],
                          table_name=row["table_name"], column_name=row["column_name"],
                          score=1.0, source="exact")
                for row in entities if row["value"] and row["value"] in query]
        return hits[:ENTITY_LIMIT]

    def _choose(self, query: str, fused: dict[str, float], candidates: list[str],
                tables: list[TableSemantic], items: list[SchemaItem], prefix: str,
                rerank: bool) -> tuple[list[tuple[str, float]], bool, str]:
        """The tables to keep: by the rerank model when there is one, else by retrieval."""
        top_n = self.settings.search_top_tables
        by_retrieval = [(table, fused[table]) for table in candidates[:top_n]]
        if not rerank or not candidates:
            return by_retrieval, False, ""
        if self.reranker is None:
            return by_retrieval, False, "没有配置重排模型，按检索排序"
        summaries = {bare_name(table.table_name, prefix): table.summary for table in tables}
        documents = [table_document(table, summaries.get(table, ""), items, prefix)
                     for table in candidates]
        try:
            with timed(self.trace, "rerank", self.reranker.model_name,
                       input_text="\n".join([query, *documents])):
                results = self.reranker.rerank(query, documents, top_n)
        except RerankError as exc:
            logger.warning("重排失败，按检索排序 error=%s", exc)
            if self.trace is not None:
                self.trace.degrade("rerank", "重排服务不可用，已沿用检索排序。")
            return by_retrieval, False, f"重排不可用：{exc}"
        return [(candidates[result.index], result.score) for result in results], True, ""


SEARCH_HEADER = "检索到的表，每张表都列出了全部字段，没列出的字段就不存在："
LEFT_OUT = "还有这些表因为长度限制没有放进来：{tables}。要用时再检索，或者用 describe_table 查看。"
NOTHING_FOUND = "没有检索到相关的表。可以换个说法再检索，或者用 describe_table 按表名查看。"


def render_search(search: TableSearch, columns: TableColumns, items: Sequence[SchemaItem],
                  summaries: dict[str, str], budget: int,
                  prefix: str) -> tuple[str, list[str]]:
    """What search_semantic returns, and the tables written out in it.

    The tables fit in ``budget`` tokens. When they do not, the chosen tables with the
    lowest scores are left out first; the tables completion added stay, as the others
    cannot be joined without them.
    """
    by_key = {(item.table_name, item.column_name): item for item in items}
    sections = {choice.table: render_table(
        choice.table, columns[choice.table], by_key, prefix,
        f"{summaries.get(choice.table, '')}（{'、'.join(choice.reasons)}）")
        for choice in search.tables if choice.table in columns}
    added = [choice.table for choice in search.tables
             if choice.table in sections and choice.reasons[0] not in (RETRIEVED, RERANKED)]
    chosen = sorted((choice for choice in search.tables if choice.table in sections
                     and choice.table not in added), key=lambda choice: -choice.score)
    kept = set(added)
    used = estimate_tokens(SEARCH_HEADER) + sum(estimate_tokens(sections[table])
                                                 for table in added)
    for choice in chosen:
        cost = estimate_tokens(sections[choice.table])
        if used + cost > budget and kept:
            continue
        kept.add(choice.table)
        used += cost
    shown = [choice.table for choice in search.tables if choice.table in kept]
    left_out = [table for table in sections if table not in kept]
    parts = [SEARCH_HEADER, *(sections[table] for table in shown)] if shown else [NOTHING_FOUND]
    if left_out:
        parts.append(LEFT_OUT.format(tables="、".join(f"{prefix}{table}" for table in left_out)))
    knowledge = RetrievalBundle(metrics=search.metrics, cases=search.cases,
                                entities=search.entities).to_prompt_context(include_schema=False)
    if knowledge:
        parts.append(knowledge)
    return "\n\n".join(parts), shown
