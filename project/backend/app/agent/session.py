from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection


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