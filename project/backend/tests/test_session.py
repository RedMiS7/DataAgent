from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agent import session
from app.agent.session import QueryRecord
from app.semantic.cli import format_answer, format_executions


class FakeConnection:
    """Records each statement; answers the column lookup of each table."""

    def __init__(self, columns: dict[str, list[str]] | None = None,
                 rows: list[Any] | None = None) -> None:
        self.columns = columns or {}
        self.rows = rows or []
        self.statements: list[tuple[str, Any]] = []

    def execute(self, statement: Any, params: Any = None) -> Any:
        sql = str(statement)
        self.statements.append((sql, params))
        if "information_schema.COLUMNS" in sql:
            return [(name,) for name in self.columns.get(params["table"], [])]
        return self.rows


OLD_QUERY_LOG = ["id", "session_id", "question", "status", "error_message", "created_at"]
OLD_TABLES = {"query_logs": OLD_QUERY_LOG,
              "call_traces": ["id", "log_id", "detail", "created_at"],
              "query_executions": ["id", "log_id", "sql_text", "status"]}


def test_old_databases_get_the_new_tables_and_columns_in_order() -> None:
    conn = FakeConnection(OLD_TABLES)
    session.ensure_log_tables(conn)  # type: ignore[arg-type]
    sqls = [sql for sql, _ in conn.statements]
    assert any("CREATE TABLE IF NOT EXISTS call_traces" in sql for sql in sqls)
    assert any("CREATE TABLE IF NOT EXISTS query_executions" in sql for sql in sqls)
    assert [sql for sql in sqls if sql.startswith("ALTER")] == [
        "ALTER TABLE query_logs ADD COLUMN summary TEXT NULL AFTER error_message",
        "ALTER TABLE query_logs ADD COLUMN summary_source VARCHAR(16) NOT NULL DEFAULT '' "
        "AFTER summary",
        "ALTER TABLE query_logs ADD COLUMN warnings JSON NULL AFTER summary_source",
        "ALTER TABLE query_logs ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'web' "
        "AFTER warnings",
        "ALTER TABLE query_logs ADD COLUMN intent JSON NULL AFTER source",
        "ALTER TABLE query_logs ADD COLUMN conclusion JSON NULL AFTER intent",
        "ALTER TABLE call_traces ADD COLUMN error_type VARCHAR(16) NOT NULL DEFAULT '' "
        "AFTER detail",
        "ALTER TABLE call_traces ADD COLUMN input_hash CHAR(16) NOT NULL DEFAULT '' "
        "AFTER error_type",
        "ALTER TABLE query_executions ADD COLUMN sql_hash CHAR(16) NOT NULL DEFAULT '' "
        "AFTER sql_text",
    ]


def test_up_to_date_databases_are_left_alone() -> None:
    conn = FakeConnection({
        "query_logs": [*OLD_QUERY_LOG, *session.QUERY_LOG_COLUMNS],
        **{table: [*OLD_TABLES[table], *(name for name, _, _ in columns)]
           for table, columns in session.LATER_COLUMNS.items()},
    })
    session.ensure_log_tables(conn)  # type: ignore[arg-type]
    assert not [sql for sql, _ in conn.statements if sql.startswith("ALTER")]


def test_the_table_statements_match_the_init_script() -> None:
    init = Path(__file__).resolve().parents[2] / "data" / "initdb" / "02_app_schema.sql"
    script = init.read_text(encoding="utf-8")
    assert session.CALL_TRACES_DDL.removesuffix(";") in script
    assert session.QUERY_EXECUTIONS_DDL.removesuffix(";") in script
    for name, definition in session.QUERY_LOG_COLUMNS.items():
        assert f"{name} " in script and definition in script


def test_query_log_stores_the_conclusion_warnings_and_source() -> None:
    class Result:
        lastrowid = 7

    conn = FakeConnection(rows=Result())  # type: ignore[arg-type]
    log_id = session.save_query_log(
        conn, "s", "问题", "", "SELECT 1", "ok", 1, 10, 1, "m", 5, 6,  # type: ignore[arg-type]
        summary="结论", summary_source="model", warnings=["警告一"], source="eval")
    params = conn.statements[0][1]
    assert log_id == 7
    assert (params["summary"], params["summary_source"], params["source"]) == (
        "结论", "model", "eval")
    assert json.loads(params["warnings"]) == ["警告一"]


def test_no_warnings_and_no_summary_are_stored_as_null() -> None:
    class Result:
        lastrowid = 1

    conn = FakeConnection(rows=Result())  # type: ignore[arg-type]
    session.save_query_log(conn, "s", "问题", "", "", "error", 0, 10, 0,  # type: ignore[arg-type]
                           "m", 0, 0, "超时")
    params = conn.statements[0][1]
    assert params["summary"] is None and params["warnings"] is None
    assert params["summary_source"] == "" and params["source"] == "web"


def test_each_query_is_one_row_with_at_most_twenty_preview_rows() -> None:
    conn = FakeConnection()
    rows = [[f"商品{index}", index] for index in range(30)]
    session.save_query_executions(conn, 3, [  # type: ignore[arg-type]
        QueryRecord(sql="DELETE FROM t", status="blocked", purpose="删", error="只允许 SELECT"),
        QueryRecord(sql="SELECT a, b FROM t", status="ok", purpose="看", query_no=1,
                    row_count=30, duration_ms=12, columns=["a", "b"], rows=rows),
    ])
    blocked, ran = conn.statements[0][1]
    assert (blocked["seq"], blocked["status"], blocked["query_no"]) == (1, "blocked", None)
    assert blocked["rows_preview"] is None and blocked["error"] == "只允许 SELECT"
    assert (ran["seq"], ran["query_no"], ran["row_count"], ran["log_id"]) == (2, 1, 30, 3)
    preview = json.loads(ran["rows_preview"])
    assert preview["columns"] == ["a", "b"] and preview["rows"] == rows[:20]


def test_no_queries_write_nothing() -> None:
    conn = FakeConnection()
    session.save_query_executions(conn, 1, [])  # type: ignore[arg-type]
    assert conn.statements == []


def test_show_trace_prints_the_conclusion_and_each_query() -> None:
    answer = format_answer({"question": "上月销量", "status": "partial", "source": "eval",
                            "duration_ms": 1500, "summary": "童装最好", "summary_source": "template",
                            "error_message": "超时", "warnings": ["时间不一致"]})
    assert answer == ["问题：上月销量", "状态 partial，来源 eval，耗时 1.5s",
                      "结论（template）：童装最好", "错误：超时", "警告：时间不一致"]
    lines = format_executions([
        {"seq": 1, "query_no": None, "purpose": "", "sql_text": "SELECT amount FROM t",
         "status": "failed", "row_count": 0, "error": "Unknown column", "duration_ms": 3,
         "rows_preview": None},
        {"seq": 2, "query_no": 1, "purpose": "上月销量", "sql_text": "SELECT a, b\nFROM t",
         "status": "ok", "row_count": 7, "error": "", "duration_ms": 9,
         "rows_preview": {"columns": ["a", "b"], "rows": [[index, index] for index in range(7)]}},
    ])
    assert lines[:3] == ["#1 [未计入] failed 3ms 0 行  -", "    SELECT amount FROM t",
                         "    错误：Unknown column"]
    assert lines[3:6] == ["#2 [查询1] ok 9ms 7 行  上月销量", "    SELECT a, b", "    FROM t"]
    assert lines[6] == "    | a | b"
    assert lines[-1] == "    | …… 另有 2 行存档"
    assert len(lines) == 13


def test_saved_calls_and_queries_carry_their_error_type_and_hashes() -> None:
    from app.agent.trace import CallRecord, text_hash
    conn = FakeConnection()
    session.save_call_traces(conn, 5, [CallRecord(  # type: ignore[arg-type]
        agent="backend", kind="rerank", name="qwen3-rerank", started_ms=1, duration_ms=2,
        ok=False, detail="HTTP 429", error_type="429", input_hash="abc")])
    session.save_query_executions(conn, 5, [QueryRecord(sql="SELECT  1", status="ok")])  # type: ignore[arg-type]
    (calls_sql, calls), (queries_sql, queries) = conn.statements
    assert "error_type, input_hash" in calls_sql
    assert (calls[0]["error_type"], calls[0]["input_hash"]) == ("429", "abc")
    assert "sql_hash" in queries_sql and queries[0]["sql_hash"] == text_hash("SELECT 1")
