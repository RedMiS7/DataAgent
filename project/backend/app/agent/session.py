from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .trace import CallRecord

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
    created_at    DATETIME NOT NULL,
    KEY idx_call_traces_log (log_id)
) ENGINE=InnoDB;"""


def log_tag(session_id: str) -> str:
    """The short form of a session id, used to follow one conversation in the logs."""
    return session_id[-6:]


def save_query_log(conn: Connection, session_id: str, question: str, rewritten: str,
                   sql_text: str, status: str, retry_count: int, duration_ms: int,
                   row_count: int, model_name: str, prompt_tokens: int,
                   completion_tokens: int, error_message: str = "") -> int:
    result = conn.execute(
        text(
            "INSERT INTO query_logs (session_id, question, rewritten_question, sql_text, status, "
            "retry_count, duration_ms, row_count, model_name, prompt_tokens, completion_tokens, "
            "error_message, created_at) VALUES (:session_id, :question, :rewritten, :sql_text, "
            ":status, :retry_count, :duration_ms, :row_count, :model_name, :prompt_tokens, "
            ":completion_tokens, :error_message, NOW())"
        ),
        {"session_id": session_id, "question": question, "rewritten": rewritten or None,
         "sql_text": sql_text, "status": status, "retry_count": retry_count,
         "duration_ms": duration_ms, "row_count": row_count, "model_name": model_name,
         "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
         "error_message": error_message[:500] if error_message else ""},
    )
    return int(result.lastrowid)

def ensure_trace_table(conn: Connection) -> None:
    conn.execute(text(CALL_TRACES_DDL))


def save_call_traces(conn: Connection, log_id: int, records: list[CallRecord]) -> None:
    if not records:
        return
    conn.execute(
        text(
            "INSERT INTO call_traces (log_id, seq, agent, kind, name, started_ms, duration_ms, "
            "input_tokens, output_tokens, ok, detail, created_at) VALUES (:log_id, :seq, :agent, "
            ":kind, :name, :started_ms, :duration_ms, :input_tokens, :output_tokens, :ok, "
            ":detail, NOW())"
        ),
        [{"log_id": log_id, "seq": seq, "agent": record.agent, "kind": record.kind,
          "name": record.name[:64], "started_ms": record.started_ms,
          "duration_ms": record.duration_ms, "input_tokens": record.input_tokens,
          "output_tokens": record.output_tokens, "ok": record.ok, "detail": record.detail}
         for seq, record in enumerate(records, start=1)],
    )


def load_call_traces(conn: Connection, log_id: int) -> list[dict]:
    rows = conn.execute(
        text("SELECT seq, agent, kind, name, started_ms, duration_ms, input_tokens, "
             "output_tokens, ok, detail FROM call_traces WHERE log_id = :log_id ORDER BY seq"),
        {"log_id": log_id},
    )
    return [dict(row._mapping) for row in rows]
