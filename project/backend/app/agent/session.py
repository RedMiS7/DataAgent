from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection


def log_tag(session_id: str) -> str:
    """The short form of a session id, used to follow one conversation in the logs."""
    return session_id[-6:]


def ensure_conversation(conn: Connection, session_id: str) -> None:
    conn.execute(
        text(
            "INSERT INTO conversations (session_id, created_at) VALUES (:session_id, NOW()) "
            "ON DUPLICATE KEY UPDATE session_id = VALUES(session_id)"
        ),
        {"session_id": session_id},
    )


def load_history(conn: Connection, session_id: str,
                 limit: int = 6) -> list[tuple[str, str]]:
    rows = conn.execute(
        text(
            "SELECT role, content FROM messages WHERE session_id = :session_id "
            "ORDER BY id DESC LIMIT :limit"
        ),
        {"session_id": session_id, "limit": limit},
    ).mappings().all()
    return [(row["role"], row["content"]) for row in reversed(rows)]


def rewind_history(conn: Connection, session_id: str, after_message_id: int) -> None:
    """Drop the messages the user replaced, so the model no longer sees them."""
    conn.execute(
        text("DELETE FROM messages WHERE session_id = :session_id AND id > :after_id"),
        {"session_id": session_id, "after_id": after_message_id},
    )


def save_message(conn: Connection, session_id: str, role: str, content: str,
                 rewritten_question: str = "", sql_text: str = "") -> int:
    result = conn.execute(
        text(
            "INSERT INTO messages (session_id, role, content, rewritten_question, sql_text, "
            "created_at) VALUES (:session_id, :role, :content, :rewritten, :sql_text, NOW())"
        ),
        {"session_id": session_id, "role": role, "content": content,
         "rewritten": rewritten_question or None, "sql_text": sql_text or None},
    )
    return int(result.lastrowid)


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