from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .trace import CallRecord, text_hash

# Same statement as data/initdb/02_app_schema.sql; run at startup for databases made before it.
CALL_TRACES_DDL = """CREATE TABLE IF NOT EXISTS call_traces (
    id            BIGINT PRIMARY KEY AUTO_INCREMENT,
    log_id        BIGINT NOT NULL,
    seq           INT NOT NULL,
    agent         VARCHAR(64) NOT NULL,
    kind          VARCHAR(16) NOT NULL,
    name          VARCHAR(64) NOT NULL,
    started_ms    INT NOT NULL,
    duration_ms   INT NOT NULL,
    input_tokens  INT NOT NULL DEFAULT 0,
    output_tokens INT NOT NULL DEFAULT 0,
    ok            TINYINT(1) NOT NULL DEFAULT 1,
    detail        VARCHAR(255) NOT NULL DEFAULT '',
    error_type    VARCHAR(16) NOT NULL DEFAULT '',
    input_hash    CHAR(16) NOT NULL DEFAULT '',
    created_at    DATETIME NOT NULL,
    KEY idx_call_traces_log (log_id)
) ENGINE=InnoDB;"""

QUERY_EXECUTIONS_DDL = """CREATE TABLE IF NOT EXISTS query_executions (
    id            BIGINT PRIMARY KEY AUTO_INCREMENT,
    log_id        BIGINT NOT NULL,
    seq           INT NOT NULL,
    query_no      INT NULL,
    purpose       VARCHAR(255) NOT NULL DEFAULT '',
    sql_text      TEXT NOT NULL,
    sql_hash      CHAR(16) NOT NULL DEFAULT '',
    status        VARCHAR(16) NOT NULL,
    row_count     INT NOT NULL DEFAULT 0,
    error         VARCHAR(512) NOT NULL DEFAULT '',
    duration_ms   INT NOT NULL DEFAULT 0,
    rows_preview  JSON NULL,
    created_at    DATETIME NOT NULL,
    KEY idx_query_executions_log (log_id)
) ENGINE=InnoDB;"""

# Columns query_logs gained after it was first created, with how to add each.
QUERY_LOG_COLUMNS = {
    "summary": "TEXT NULL",
    "summary_source": "VARCHAR(16) NOT NULL DEFAULT ''",
    "warnings": "JSON NULL",
    "source": "VARCHAR(16) NOT NULL DEFAULT 'web'",
    "intent": "JSON NULL",
    "conclusion": "JSON NULL",
}
# Columns the call and query archives gained later: (column, definition, after which).
LATER_COLUMNS = {
    "call_traces": [("error_type", "VARCHAR(16) NOT NULL DEFAULT ''", "detail"),
                    ("input_hash", "CHAR(16) NOT NULL DEFAULT ''", "error_type")],
    "query_executions": [("sql_hash", "CHAR(16) NOT NULL DEFAULT ''", "sql_text")],
}

PREVIEW_ROWS = 20

LogSource = Literal["web", "eval"]


@dataclass(slots=True)
class QueryRecord:
    """One SQL a question tried, kept so a reviewer sees every step of the answer.

    status: ok (ran), blocked (stopped by the checks before running) or failed (the
    database returned an error). query_no is the [查询n] number of a query that ran.
    """
    sql: str
    status: str
    purpose: str = ""
    query_no: int | None = None
    row_count: int = 0
    error: str = ""
    duration_ms: int = 0
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)


def log_tag(session_id: str) -> str:
    """The short form of a session id, used to follow one conversation in the logs."""
    return session_id[-6:]


def save_query_log(conn: Connection, session_id: str, question: str, rewritten: str,
                   sql_text: str, status: str, retry_count: int, duration_ms: int,
                   row_count: int, model_name: str, prompt_tokens: int,
                   completion_tokens: int, error_message: str = "", *, summary: str = "",
                   summary_source: str = "", warnings: list[str] | None = None,
                   source: LogSource = "web", intent: dict[str, Any] | None = None,
                   conclusion: dict[str, Any] | None = None) -> int:
    result = conn.execute(
        text(
            "INSERT INTO query_logs (session_id, question, rewritten_question, sql_text, status, "
            "retry_count, duration_ms, row_count, model_name, prompt_tokens, completion_tokens, "
            "error_message, summary, summary_source, warnings, source, intent, conclusion, "
            "created_at) VALUES (:session_id, :question, :rewritten, :sql_text, :status, "
            ":retry_count, :duration_ms, :row_count, :model_name, :prompt_tokens, "
            ":completion_tokens, :error_message, :summary, :summary_source, :warnings, :source, "
            ":intent, :conclusion, NOW())"
        ),
        {"session_id": session_id, "question": question, "rewritten": rewritten or None,
         "sql_text": sql_text, "status": status, "retry_count": retry_count,
         "duration_ms": duration_ms, "row_count": row_count, "model_name": model_name,
         "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
         "error_message": error_message[:500] if error_message else "",
         "summary": summary or None, "summary_source": summary_source,
         "warnings": json.dumps(warnings, ensure_ascii=False) if warnings else None,
         "source": source,
         "intent": json.dumps(intent, ensure_ascii=False) if intent else None,
         "conclusion": json.dumps(conclusion, ensure_ascii=False, default=str)
         if conclusion else None},
    )
    return int(result.lastrowid)


def _columns(conn: Connection, table: str) -> set[str]:
    return {row[0] for row in conn.execute(text(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = :table"), {"table": table})}


def ensure_log_tables(conn: Connection) -> None:
    """Add the log tables and columns that databases made before them lack."""
    conn.execute(text(CALL_TRACES_DDL))
    conn.execute(text(QUERY_EXECUTIONS_DDL))
    existing = _columns(conn, "query_logs")
    previous = "error_message"
    for name, definition in QUERY_LOG_COLUMNS.items():
        if name not in existing:
            conn.execute(text(f"ALTER TABLE query_logs ADD COLUMN {name} {definition} "
                              f"AFTER {previous}"))
        previous = name
    for table, columns in LATER_COLUMNS.items():
        existing = _columns(conn, table)
        for name, definition, after in columns:
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition} "
                                  f"AFTER {after}"))


def save_query_executions(conn: Connection, log_id: int, records: list[QueryRecord]) -> None:
    if not records:
        return
    conn.execute(
        text(
            "INSERT INTO query_executions (log_id, seq, query_no, purpose, sql_text, sql_hash, "
            "status, row_count, error, duration_ms, rows_preview, created_at) VALUES (:log_id, "
            ":seq, :query_no, :purpose, :sql_text, :sql_hash, :status, :row_count, :error, "
            ":duration_ms, :rows_preview, NOW())"
        ),
        [{"log_id": log_id, "seq": seq, "query_no": record.query_no,
          "purpose": record.purpose[:255], "sql_text": record.sql,
          "sql_hash": text_hash(record.sql), "status": record.status,
          "row_count": record.row_count, "error": record.error[:500],
          "duration_ms": record.duration_ms, "rows_preview": _preview(record)}
         for seq, record in enumerate(records, start=1)],
    )


def _preview(record: QueryRecord) -> str | None:
    if not record.columns:
        return None
    return json.dumps({"columns": record.columns, "rows": record.rows[:PREVIEW_ROWS]},
                      ensure_ascii=False, default=str)


def load_query_log(conn: Connection, log_id: int) -> dict | None:
    row = conn.execute(
        text("SELECT id, question, status, source, summary, summary_source, warnings, "
             "intent, conclusion, error_message, duration_ms FROM query_logs WHERE id = :log_id"),
        {"log_id": log_id},
    ).first()
    if row is None:
        return None
    log = dict(row._mapping)
    log["warnings"] = _json_value(log["warnings"]) or []
    log["intent"] = _json_value(log["intent"]) or {}
    log["conclusion"] = _json_value(log["conclusion"]) or {}
    return log


def load_query_executions(conn: Connection, log_id: int) -> list[dict]:
    rows = conn.execute(
        text("SELECT seq, query_no, purpose, sql_text, sql_hash, status, row_count, error, "
             "duration_ms, rows_preview FROM query_executions WHERE log_id = :log_id "
             "ORDER BY seq"),
        {"log_id": log_id},
    )
    records = []
    for row in rows:
        record = dict(row._mapping)
        record["rows_preview"] = _json_value(record["rows_preview"])
        records.append(record)
    return records


def _json_value(value: Any) -> Any:
    """JSON columns come back as text from some drivers and parsed from others."""
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def save_call_traces(conn: Connection, log_id: int, records: list[CallRecord]) -> None:
    if not records:
        return
    conn.execute(
        text(
            "INSERT INTO call_traces (log_id, seq, agent, kind, name, started_ms, duration_ms, "
            "input_tokens, output_tokens, ok, detail, error_type, input_hash, created_at) VALUES "
            "(:log_id, :seq, :agent, :kind, :name, :started_ms, :duration_ms, :input_tokens, "
            ":output_tokens, :ok, :detail, :error_type, :input_hash, NOW())"
        ),
        [{"log_id": log_id, "seq": seq, "agent": record.agent, "kind": record.kind,
          "name": record.name[:64], "started_ms": record.started_ms,
          "duration_ms": record.duration_ms, "input_tokens": record.input_tokens,
          "output_tokens": record.output_tokens, "ok": record.ok, "detail": record.detail,
          "error_type": record.error_type, "input_hash": record.input_hash}
         for seq, record in enumerate(records, start=1)],
    )


def load_call_traces(conn: Connection, log_id: int) -> list[dict]:
    rows = conn.execute(
        text("SELECT seq, agent, kind, name, started_ms, duration_ms, input_tokens, "
             "output_tokens, ok, detail, error_type, input_hash FROM call_traces "
             "WHERE log_id = :log_id ORDER BY seq"),
        {"log_id": log_id},
    )
    return [dict(row._mapping) for row in rows]
