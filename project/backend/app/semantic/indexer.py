from __future__ import annotations

from sqlalchemy.engine import Connection

from ..config import Settings
from ..embedding.base import EmbeddingProvider
from ..vector.base import VectorRecord, VectorStore
from . import repository
from .models import CaseHit, Metric, SchemaItem

BATCH = 128


def _schema_text(item: SchemaItem) -> str:
    parts = [f"{item.table_name}.{item.column_name}", item.description]
    if item.synonyms:
        parts.append(f"同义词：{item.synonyms}")
    if item.join_hint:
        parts.append(item.join_hint)
    return " ".join(parts)


def _metric_text(metric: Metric) -> str:
    parts = [metric.name, metric.definition]
    if metric.aliases:
        parts.append(f"别名：{metric.aliases}")
    return " ".join(parts)


def _embed_and_upsert(store: VectorStore, collection: str, embedder: EmbeddingProvider,
                      records: list[tuple[str, str, dict[str, object]]]) -> int:
    if not records:
        return 0
    store.ensure_collection(collection, embedder.dim)
    written = 0
    for start in range(0, len(records), BATCH):
        chunk = records[start:start + BATCH]
        vectors = embedder.embed([text for _, text, _ in chunk])
        store.upsert(collection, [
            VectorRecord(id=record_id, vector=vector, payload=payload)
            for (record_id, _, payload), vector in zip(chunk, vectors, strict=False)
        ])
        written += len(chunk)
    return written


def build_index(settings: Settings, app_conn: Connection, business_conn: Connection,
                store: VectorStore, embedder: EmbeddingProvider) -> dict[str, int]:
    schema_items = repository.fetch_schema_items(app_conn)
    metrics = repository.fetch_metrics(app_conn)
    cases = repository.fetch_cases(app_conn)
    entities = repository.fetch_entity_values(business_conn)

    collections = {
        "schema_items": settings.collection_name("schema_items"),
        "metrics": settings.collection_name("metrics"),
        "qa_cases": settings.collection_name("qa_cases"),
        "entity_values": settings.collection_name("entity_values"),
    }
    for name in collections.values():
        store.recreate_collection(name, embedder.dim)

    model_name = embedder.model_name
    schema_records = [
        (f"schema:{item.table_name}.{item.column_name}", _schema_text(item),
         {"table": item.table_name, "column": item.column_name, "model": model_name})
        for item in schema_items
    ]
    metric_records = [
        (f"metric:{metric.name}", _metric_text(metric),
         {"name": metric.name, "model": model_name})
        for metric in metrics
    ]
    case_records: list[tuple[str, str, dict[str, object]]] = [
        (f"case:{case.id}", case.question,
         {"case_id": case.id, "sql": case.sql_text, "model": model_name})
        for case in cases
    ]
    entity_records = [
        (f"entity:{row['entity_type']}:{row['value']}", row["value"],
         {"entity_type": row["entity_type"], "value": row["value"], "model": model_name})
        for row in entities
    ]

    return {
        "schema_items": _embed_and_upsert(store, collections["schema_items"], embedder, schema_records),
        "metrics": _embed_and_upsert(store, collections["metrics"], embedder, metric_records),
        "qa_cases": _embed_and_upsert(store, collections["qa_cases"], embedder, case_records),
        "entity_values": _embed_and_upsert(store, collections["entity_values"], embedder, entity_records),
    }


__all__ = ["CaseHit", "build_index"]