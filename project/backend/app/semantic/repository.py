from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .models import CaseHit, Metric, SchemaItem, TableSemantic


@dataclass(slots=True)
class SeedBundle:
    tables: list[TableSemantic] = field(default_factory=list)
    schema_items: list[SchemaItem] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)

    @property
    def schema_version(self) -> str:
        versions = {item.schema_version for item in self.schema_items}
        return sorted(versions)[-1] if versions else "v1"


def _read_json(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"seed file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_seed(seed_dir: Path) -> SeedBundle:
    tables = [TableSemantic.model_validate(row) for row in _read_json(seed_dir / "semantic_tables.json")]
    items = [SchemaItem.model_validate(row) for row in _read_json(seed_dir / "schema_items.json")]
    metrics = [Metric.model_validate(row) for row in _read_json(seed_dir / "metrics.json")]
    return SeedBundle(tables=tables, schema_items=items, metrics=metrics)


def sync_seed(conn: Connection, bundle: SeedBundle) -> dict[str, int]:
    for table in bundle.tables:
        conn.execute(
            text(
                "INSERT INTO semantic_tables (table_name, summary, schema_version, enabled, updated_at) "
                "VALUES (:table_name, :summary, :schema_version, 1, NOW()) "
                "ON DUPLICATE KEY UPDATE summary=VALUES(summary), "
                "schema_version=VALUES(schema_version), enabled=1, updated_at=NOW()"
            ),
            table.model_dump(),
        )
    for item in bundle.schema_items:
        conn.execute(
            text(
                "INSERT INTO semantic_schema_items (table_name, column_name, description, synonyms, "
                "join_hint, schema_version, enabled, updated_at) "
                "VALUES (:table_name, :column_name, :description, :synonyms, :join_hint, "
                ":schema_version, 1, NOW()) "
                "ON DUPLICATE KEY UPDATE description=VALUES(description), synonyms=VALUES(synonyms), "
                "join_hint=VALUES(join_hint), schema_version=VALUES(schema_version), "
                "enabled=1, updated_at=NOW()"
            ),
            item.model_dump(),
        )
    for metric in bundle.metrics:
        conn.execute(
            text(
                "INSERT INTO semantic_metrics (name, aliases, definition, formula, related_tables, "
                "enabled, updated_at) VALUES (:name, :aliases, :definition, :formula, "
                ":related_tables, :enabled, NOW()) "
                "ON DUPLICATE KEY UPDATE aliases=VALUES(aliases), definition=VALUES(definition), "
                "formula=VALUES(formula), related_tables=VALUES(related_tables), "
                "enabled=VALUES(enabled), updated_at=NOW()"
            ),
            metric.model_dump(),
        )
    conn.execute(
        text(
            "INSERT INTO app_meta (meta_key, meta_value, updated_at) VALUES ('schema_version', :v, NOW()) "
            "ON DUPLICATE KEY UPDATE meta_value=VALUES(meta_value), updated_at=NOW()"
        ),
        {"v": bundle.schema_version},
    )
    return {
        "tables": len(bundle.tables),
        "schema_items": len(bundle.schema_items),
        "metrics": len(bundle.metrics),
    }


def fetch_tables(conn: Connection, enabled_only: bool = True) -> list[TableSemantic]:
    where = "WHERE enabled = 1" if enabled_only else ""
    rows = conn.execute(text(
        f"SELECT table_name, summary, schema_version FROM semantic_tables {where} "
        "ORDER BY table_name"
    )).mappings().all()
    return [TableSemantic(**dict(row)) for row in rows]


def fetch_schema_items(conn: Connection, enabled_only: bool = True) -> list[SchemaItem]:
    where = "WHERE enabled = 1" if enabled_only else ""
    rows = conn.execute(text(
        f"SELECT table_name, column_name, description, synonyms, join_hint, schema_version "
        f"FROM semantic_schema_items {where} ORDER BY table_name, column_name"
    )).mappings().all()
    return [SchemaItem(**dict(row)) for row in rows]


def fetch_metrics(conn: Connection, enabled_only: bool = True) -> list[Metric]:
    where = "WHERE enabled = 1" if enabled_only else ""
    rows = conn.execute(text(
        f"SELECT name, aliases, definition, formula, related_tables, enabled "
        f"FROM semantic_metrics {where} ORDER BY name"
    )).mappings().all()
    return [Metric(**dict(row)) for row in rows]


def fetch_cases(conn: Connection, status: str = "approved") -> list[CaseHit]:
    rows = conn.execute(text(
        "SELECT id, question, sql_text FROM qa_cases WHERE status = :status ORDER BY id"
    ), {"status": status}).mappings().all()
    return [CaseHit(id=row["id"], question=row["question"], sql_text=row["sql_text"]) for row in rows]


def fetch_entity_values(business_conn: Connection) -> list[dict[str, str]]:
    queries = [
        ("product", "SELECT DISTINCT name FROM safe.products WHERE status = 1"),
        ("category", "SELECT DISTINCT name FROM safe.categories"),
        ("channel", "SELECT DISTINCT name FROM safe.channels"),
        ("province", "SELECT DISTINCT province FROM safe.orders"),
    ]
    values: list[dict[str, str]] = []
    for entity_type, sql in queries:
        for (value,) in business_conn.execute(text(sql)).all():
            if value:
                values.append({"entity_type": entity_type, "value": str(value)})
    return values


def insert_feedback(conn: Connection, log_id: int, verdict: str) -> None:
    conn.execute(
        text(
            "INSERT INTO feedback (log_id, verdict, created_at) VALUES (:log_id, :verdict, NOW()) "
            "ON DUPLICATE KEY UPDATE verdict=VALUES(verdict), created_at=NOW()"
        ),
        {"log_id": log_id, "verdict": verdict},
    )
    if verdict != "correct":
        return
    log = conn.execute(
        text(
            "SELECT question, sql_text FROM query_logs WHERE id = :log_id AND sql_text <> ''"
        ),
        {"log_id": log_id},
    ).mappings().first()
    if not log:
        return
    conn.execute(
        text(
            "INSERT INTO qa_cases (question, sql_text, result_summary, status, source_log_id, created_at) "
            "VALUES (:question, :sql_text, '', 'pending', :log_id, NOW())"
        ),
        {"question": log["question"], "sql_text": log["sql_text"], "log_id": log_id},
    )


def list_cases(conn: Connection, status: str | None = None) -> list[dict]:
    sql = (
        "SELECT id, question, sql_text, result_summary, status, created_at FROM qa_cases "
        f"{'WHERE status = :status' if status else ''} ORDER BY id DESC"
    )
    rows = conn.execute(text(sql), {"status": status} if status else {}).mappings().all()
    return [
        {
            "id": row["id"],
            "question": row["question"],
            "sql_text": row["sql_text"],
            "result_summary": row["result_summary"],
            "status": row["status"],
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def review_case(conn: Connection, case_id: int, status: str) -> bool:
    result = conn.execute(
        text("UPDATE qa_cases SET status = :status, reviewed_at = NOW() WHERE id = :id"),
        {"status": status, "id": case_id},
    )
    return result.rowcount > 0