"""Evaluation runs and their case results in the app database.

Every case result is written as soon as it is scored, so a run that stops can be
resumed and looked at. Only one run at a time per app database: a run holds a MySQL
named lock for as long as it goes on, and the lock goes away with its connection
when the process dies.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..config import Settings
from ..db.engine import eval_app_engine
from .runner import CaseResult

LOCK_PREFIX = "dataagent_eval:"
LEFTOVER_ERROR = "评测进程中途退出，下一次评测启动时标为失败"


class EvalBusy(RuntimeError):
    """Another evaluation holds this app database's lock."""


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def _parsed(value: Any) -> Any:
    """JSON columns come back as text from some drivers and parsed from others."""
    return json.loads(value) if isinstance(value, (str, bytes)) else value


class RunStore(Protocol):
    """What a run needs from storage; tests use an in-memory one."""

    def create_run(self, *, dataset: str, mode: str, categories: list[str],
                   options: dict[str, Any], repeat: int, concurrency: int, llm_model: str,
                   schema_mode: str, git_commit: str) -> int: ...

    def reopen_run(self, run_id: int, concurrency: int) -> None: ...

    def set_total(self, run_id: int, total: int) -> None: ...

    def save_result(self, run_id: int, result: CaseResult) -> None: ...

    def results(self, run_id: int) -> list[CaseResult]: ...

    def finish(self, run_id: int, status: str, summary: dict | None = None,
               error: str = "") -> None: ...


class EvalStore:
    """Runs and results, read and written with the evaluation account."""

    def __init__(self, settings: Settings, engine: Engine | None = None) -> None:
        self.settings = settings
        self.engine = engine or eval_app_engine(settings)
        self.lock_name = (LOCK_PREFIX + settings.mysql_app_db)[:64]

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Hold this app database's evaluation lock, or raise EvalBusy with the run
        that holds it. Runs left ``running`` by a process that died are marked failed."""
        with self.engine.connect() as conn:
            got = conn.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": self.lock_name}).scalar()
            if got != 1:
                running = self.running_runs()
                which = f"评测 {running[0]['id']}" if running else "另一个评测"
                raise EvalBusy(f"{which} 正在运行（{self.settings.mysql_app_db}），"
                               "同一时间只能运行一个评测，请等它结束")
            try:
                self.fail_leftovers()
                yield
            finally:
                conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": self.lock_name})

    def running_runs(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, started_at FROM eval_runs WHERE status = 'running' ORDER BY id DESC"))
            return [dict(row._mapping) for row in rows]

    def fail_leftovers(self) -> int:
        """Mark runs still ``running`` as failed. Only called while holding the lock,
        so none of them is really running."""
        with self.engine.begin() as conn:
            result = conn.execute(text(
                "UPDATE eval_runs SET status = 'failed', error = :error, finished_at = NOW() "
                "WHERE status = 'running'"), {"error": LEFTOVER_ERROR})
            return int(result.rowcount or 0)

    def create_run(self, *, dataset: str, mode: str, categories: list[str],
                   options: dict[str, Any], repeat: int, concurrency: int, llm_model: str,
                   schema_mode: str, git_commit: str) -> int:
        with self.engine.begin() as conn:
            result = conn.execute(text(
                "INSERT INTO eval_runs (dataset, mode, categories, options, repeat_count, "
                "concurrency, status, llm_model, schema_mode, git_commit, started_at) VALUES "
                "(:dataset, :mode, :categories, :options, :repeat, :concurrency, 'running', "
                ":llm_model, :schema_mode, :git_commit, NOW())"),
                {"dataset": dataset, "mode": mode, "categories": _json(categories),
                 "options": _json(options), "repeat": repeat, "concurrency": concurrency,
                 "llm_model": llm_model[:64], "schema_mode": schema_mode[:16],
                 "git_commit": git_commit[:40]})
            return int(result.lastrowid)

    def reopen_run(self, run_id: int, concurrency: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE eval_runs SET status = 'running', error = NULL, finished_at = NULL, "
                "concurrency = :concurrency WHERE id = :id"),
                {"id": run_id, "concurrency": concurrency})

    def set_total(self, run_id: int, total: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE eval_runs SET total = :total WHERE id = :id"),
                         {"id": run_id, "total": total})

    def save_result(self, run_id: int, result: CaseResult) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO eval_case_results (run_id, case_id, attempt, dataset, kind, "
                "category, passed, scored, detail, failure_hint, sql_text, summary, "
                "summary_source, warnings, log_id, input_tokens, output_tokens, duration_ms, "
                "result, created_at) VALUES (:run_id, :case_id, :attempt, :dataset, :kind, "
                ":category, :passed, :scored, :detail, :failure_hint, :sql_text, :summary, "
                ":summary_source, :warnings, :log_id, :input_tokens, :output_tokens, "
                ":duration_ms, :result, NOW())"),
                {"run_id": run_id, "case_id": result.id[:64], "attempt": result.attempt,
                 "dataset": result.dataset[:64], "kind": result.kind[:32],
                 "category": result.category[:32], "passed": result.passed,
                 "scored": result.scored, "detail": result.detail,
                 "failure_hint": result.failure_hint[:64], "sql_text": result.sql or None,
                 "summary": result.summary or None, "summary_source": result.summary_source,
                 "warnings": _json(result.warnings) if result.warnings else None,
                 "log_id": result.log_id, "input_tokens": result.input_tokens,
                 "output_tokens": result.output_tokens, "duration_ms": result.duration_ms,
                 "result": _json(asdict(result))})
            conn.execute(text("UPDATE eval_runs SET done = done + 1 WHERE id = :id"),
                         {"id": run_id})

    def results(self, run_id: int) -> list[CaseResult]:
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT result FROM eval_case_results WHERE run_id = :id ORDER BY id"),
                {"id": run_id})
            return [CaseResult.from_dict(_parsed(row[0])) for row in rows]

    def finish(self, run_id: int, status: str, summary: dict | None = None,
               error: str = "") -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE eval_runs SET status = :status, summary = :summary, error = :error, "
                "finished_at = NOW() WHERE id = :id"),
                {"id": run_id, "status": status, "summary": _json(summary),
                 "error": error or None})

    def run(self, run_id: int) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM eval_runs WHERE id = :id"),
                               {"id": run_id}).first()
        if row is None:
            return None
        run = dict(row._mapping)
        for name in ("categories", "options", "summary"):
            run[name] = _parsed(run[name])
        return run

    def runs(self, limit: int = 20) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, dataset, mode, repeat_count, concurrency, status, done, total, "
                "llm_model, git_commit, summary, started_at, finished_at FROM eval_runs "
                "ORDER BY id DESC LIMIT :limit"), {"limit": limit})
            runs = [dict(row._mapping) for row in rows]
        for run in runs:
            run["summary"] = _parsed(run["summary"])
        return runs
