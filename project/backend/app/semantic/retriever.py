from __future__ import annotations

from sqlalchemy.engine import Connection

from ..config import Settings
from ..db.engine import app_connection, business_connection
from ..embedding.base import EmbeddingProvider
from ..vector.base import VectorHit, VectorStore, VectorStoreError
from . import repository
from .models import (
    CaseHit,
    EntityHit,
    Metric,
    MetricHit,
    RetrievalBundle,
    SchemaHit,
    SchemaItem,
)


def _bigrams(text: str) -> set[str]:
    normalized = " " + text.strip() + " "
    return {normalized[i:i + 2] for i in range(len(normalized) - 1)}


def _overlap(question: str, text: str) -> float:
    left = _bigrams(question)
    right = _bigrams(text)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left)


def _keyword_rank(question: str, items: list[tuple[object, str]], top_k: int) -> list[tuple[object, float]]:
    scored = [(_overlap(question, text), index, item) for index, (item, text) in enumerate(items)]
    scored.sort(key=lambda row: (row[0], -row[1]), reverse=True)
    return [(item, score) for score, _, item in scored if score > 0][:top_k]


class SemanticRetriever:
    def __init__(self, settings: Settings, store: VectorStore | None,
                 embedder: EmbeddingProvider | None) -> None:
        self.settings = settings
        self.store = store
        self.embedder = embedder

    def retrieve(self, question: str) -> RetrievalBundle:
        with app_connection(self.settings) as conn:
            return self.retrieve_with_connection(question, conn)

    def retrieve_with_connection(self, question: str, conn: Connection) -> RetrievalBundle:
        bundle = RetrievalBundle()
        bundle.tables = repository.fetch_tables(conn)
        schema_items = repository.fetch_schema_items(conn)
        metrics = repository.fetch_metrics(conn)
        cases = repository.fetch_cases(conn)
        with business_connection(self.settings) as business_conn:
            entities = repository.fetch_entity_values(business_conn, schema_items)

        if self._uses_keyword_only():
            return self._keyword_fallback(question, bundle, schema_items, metrics, cases, entities)

        try:
            bundle = self._vector_retrieve(question, bundle, schema_items, metrics, cases, entities)
            return self._fill_missing_with_keyword(question, bundle, schema_items, metrics, cases)
        except Exception as exc:
            fallback = self._keyword_fallback(question, bundle, schema_items, metrics, cases, entities)
            fallback.degraded = True
            fallback.degraded_reason = f"向量检索不可用：{exc}"
            return fallback

    def _uses_keyword_only(self) -> bool:
        if self.store is None or self.embedder is None:
            return True
        model_name = getattr(self.embedder, "model_name", "")
        return model_name.startswith("fake")

    def _fill_missing_with_keyword(self, question: str, bundle: RetrievalBundle,
                                   schema_items: list[SchemaItem], metrics: list[Metric],
                                   cases: list[CaseHit]) -> RetrievalBundle:
        top_k = self.settings.retrieval_top_k
        if not bundle.schema_items:
            for item, score in _keyword_rank(
                question,
                [(item, f"{item.table_name}.{item.column_name} {item.description} "
                        f"{item.synonyms} {item.join_hint}") for item in schema_items],
                top_k,
            ):
                bundle.schema_items.append(SchemaHit(item=item, score=score, source="keyword"))
        if not bundle.metrics:
            for metric, score in _keyword_rank(
                question,
                [(metric, f"{metric.name} {metric.aliases} {metric.definition}")
                 for metric in metrics],
                top_k,
            ):
                bundle.metrics.append(MetricHit(metric=metric, score=score, source="keyword"))
        if not bundle.cases:
            for case, score in _keyword_rank(
                question, [(case, case.question) for case in cases], top_k):
                bundle.cases.append(CaseHit(id=case.id, question=case.question,
                                            sql_text=case.sql_text, score=score,
                                            source="keyword"))
        bundle.schema_items = self._ensure_schema_coverage(bundle, schema_items)
        return bundle

    def _ensure_schema_coverage(self, bundle: RetrievalBundle,
                                schema_items: list[SchemaItem]) -> list[SchemaHit]:
        if bundle.schema_items or not bundle.metrics:
            return bundle.schema_items
        tables: set[str] = set()
        for hit in bundle.metrics[:2]:
            tables.update(part.strip() for part in hit.metric.related_tables.split(",")
                          if part.strip())
        covered = sorted(
            (item for item in schema_items if item.table_name in tables),
            key=lambda item: (item.table_name, item.column_name),
        )
        return [SchemaHit(item=item, score=0.0, source="metric_table") for item in covered][:25]

    def _vector_retrieve(self, question: str, bundle: RetrievalBundle,
                         schema_items: list[SchemaItem], metrics: list[Metric],
                         cases: list[CaseHit], entities: list[dict[str, str]]) -> RetrievalBundle:
        assert self.store is not None and self.embedder is not None
        vector = self.embedder.embed([question])[0]
        threshold = self.settings.retrieval_min_score
        top_k = self.settings.retrieval_top_k

        schema_by_key = {(item.table_name, item.column_name): item for item in schema_items}
        metric_by_name = {metric.name: metric for metric in metrics}
        case_by_id = {case.id: case for case in cases}

        for hit in self.store.search(self.settings.collection_name("schema_items"), vector, top_k):
            item = schema_by_key.get((str(hit.payload.get("table")), str(hit.payload.get("column"))))
            if item is not None and hit.score >= threshold:
                bundle.schema_items.append(SchemaHit(item=item, score=hit.score))

        for hit in self.store.search(self.settings.collection_name("metrics"), vector, top_k):
            metric = metric_by_name.get(str(hit.payload.get("name")))
            if metric is not None and hit.score >= threshold:
                bundle.metrics.append(MetricHit(metric=metric, score=hit.score))

        for hit in self.store.search(self.settings.collection_name("qa_cases"), vector, top_k):
            case = case_by_id.get(int(hit.payload.get("case_id", 0)))
            if case is not None and hit.score >= threshold:
                bundle.cases.append(CaseHit(id=case.id, question=case.question,
                                            sql_text=case.sql_text, score=hit.score))

        bundle.entities = self._match_entities(question, vector, entities)
        return bundle

    def _match_entities(self, question: str, vector: list[float],
                        entities: list[dict[str, str]]) -> list[EntityHit]:
        hits: list[EntityHit] = []
        seen: set[tuple[str, str]] = set()
        for row in entities:
            value = row["value"]
            if value and value in question:
                key = (row["entity_type"], value)
                if key not in seen:
                    seen.add(key)
                    hits.append(EntityHit(entity_type=row["entity_type"], value=value,
                                          table_name=row["table_name"],
                                          column_name=row["column_name"],
                                          score=1.0, source="exact"))

        if self.store is None or not hits:
            try:
                assert self.store is not None
                vector_hits: list[VectorHit] = self.store.search(
                    self.settings.collection_name("entity_values"), vector,
                    self.settings.retrieval_top_k)
            except VectorStoreError:
                vector_hits = []
            for hit in vector_hits:
                if hit.score < self.settings.retrieval_min_score:
                    continue
                key = (str(hit.payload.get("entity_type")), str(hit.payload.get("value")))
                if key in seen:
                    continue
                seen.add(key)
                hits.append(EntityHit(entity_type=key[0], value=key[1],
                                      table_name=str(hit.payload.get("table_name", "")),
                                      column_name=str(hit.payload.get("column_name", "")),
                                      score=hit.score, source="vector"))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:5]

    def _keyword_fallback(self, question: str, bundle: RetrievalBundle,
                          schema_items: list[SchemaItem], metrics: list[Metric],
                          cases: list[CaseHit], entities: list[dict[str, str]]) -> RetrievalBundle:
        top_k = self.settings.retrieval_top_k
        schema_ranked = _keyword_rank(
            question,
            [(item, f"{item.table_name}.{item.column_name} {item.description} {item.synonyms} "
                    f"{item.join_hint}") for item in schema_items],
            top_k,
        )
        for item, score in schema_ranked:
            bundle.schema_items.append(SchemaHit(item=item, score=score, source="keyword"))

        metric_ranked = _keyword_rank(
            question,
            [(metric, f"{metric.name} {metric.aliases} {metric.definition}") for metric in metrics],
            top_k,
        )
        for metric, score in metric_ranked:
            bundle.metrics.append(MetricHit(metric=metric, score=score, source="keyword"))

        case_ranked = _keyword_rank(
            question, [(case, case.question) for case in cases], top_k)
        for case, score in case_ranked:
            bundle.cases.append(CaseHit(id=case.id, question=case.question,
                                        sql_text=case.sql_text, score=score, source="keyword"))

        for row in entities:
            if row["value"] and row["value"] in question:
                bundle.entities.append(EntityHit(entity_type=row["entity_type"],
                                                 value=row["value"],
                                                 table_name=row["table_name"],
                                                 column_name=row["column_name"], score=1.0,
                                                 source="keyword"))
        if bundle.entities:
            bundle.entities = bundle.entities[:5]
        bundle.schema_items = self._ensure_schema_coverage(bundle, schema_items)
        bundle.degraded = True
        bundle.degraded_reason = "未配置向量检索，使用关键词匹配"
        return bundle