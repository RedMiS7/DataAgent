"""Runs of plan 1.3 and 1.5: results saved one by one, resume, repeat and concurrency,
one run at a time, and the two entry points."""

from __future__ import annotations

import importlib.util
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from app.api.schemas import AnswerPayload, HistoryMessage, TableBlock
from app.config import Settings
from app.evaluation import run as run_module
from app.evaluation import runner
from app.evaluation.__main__ import export_payload
from app.evaluation.run import RunOptions, build_jobs, execute
from app.evaluation.runner import CaseResult, Job
from app.evaluation.store import EvalBusy, EvalStore

PROJECT_DIR = Path(__file__).resolve().parents[2]


class MemoryStore:
    """The RunStore a run uses, in memory."""

    def __init__(self, earlier: list[CaseResult] | None = None) -> None:
        self.saved: list[CaseResult] = list(earlier or [])
        self.total = 0
        self.closed: tuple[str, dict | None, str] | None = None
        self.lock = threading.Lock()

    def set_total(self, run_id: int, total: int) -> None:
        self.total = total

    def save_result(self, run_id: int, result: CaseResult) -> None:
        with self.lock:
            self.saved.append(result)

    def results(self, run_id: int) -> list[CaseResult]:
        return list(self.saved)

    def finish(self, run_id: int, status: str, summary: dict | None = None,
               error: str = "") -> None:
        self.closed = (status, summary, error)


def _job(case_id: str, outcomes: dict[int, bool] | None = None,
         calls: list[tuple[str, int]] | None = None) -> Job:
    def run(attempt: int) -> CaseResult:
        if calls is not None:
            calls.append((case_id, attempt))
        passed = (outcomes or {}).get(attempt, True)
        return CaseResult(id=case_id, category="metric", question=case_id, passed=passed,
                          dataset="d", kind="data", attempt=attempt, duration_ms=5)
    return Job(id=case_id, category="metric", question=case_id,
               meta={"dataset": "d", "kind": "data"}, run=run)


def test_each_result_is_saved_and_the_run_closes_with_its_scores() -> None:
    store = MemoryStore()
    seen: list[tuple[str, int, int]] = []
    jobs = [_job("q1"), _job("q2", {1: False}), _job("q3")]

    report = execute(store, 1, jobs, RunOptions(concurrency=2),
                     lambda result, done, total: seen.append((result.id, done, total)))

    assert store.total == 3
    assert sorted(item[1] for item in seen) == [1, 2, 3] and {item[2] for item in seen} == {3}
    assert [case.id for case in report.cases] == ["q1", "q2", "q3"]
    status, summary, _ = store.closed  # type: ignore[misc]
    assert status == "done"
    assert summary["groups"]["d/data"]["passed"] == 2 and "cases" not in summary


def test_repeat_runs_every_case_n_times_and_scores_the_spread() -> None:
    store = MemoryStore()
    jobs = [_job("q1"), _job("q2", {2: False})]

    report = execute(store, 1, jobs, RunOptions(repeat=3, concurrency=4))

    assert store.total == 6 and len(store.saved) == 6
    assert [(case.id, case.attempt) for case in report.cases] == [
        ("q1", 1), ("q1", 2), ("q1", 3), ("q2", 1), ("q2", 2), ("q2", 3)]
    stats = store.closed[1]["repeat"]["d/data"]  # type: ignore[index]
    assert stats["accuracy"] == [1.0, 0.5, 1.0] and stats["pass_all"] == 0.5


def test_resume_skips_saved_cases_and_keeps_them_in_the_report() -> None:
    earlier = [CaseResult(id="q1", category="metric", question="q1", passed=False,
                          dataset="d", kind="data", attempt=1)]
    store = MemoryStore(earlier)
    calls: list[tuple[str, int]] = []
    jobs = [_job("q1", calls=calls), _job("q2", calls=calls)]

    report = execute(store, 1, jobs, RunOptions(repeat=2, concurrency=1))

    assert sorted(calls) == [("q1", 2), ("q2", 1), ("q2", 2)]
    assert [(case.id, case.attempt, case.passed) for case in report.cases] == [
        ("q1", 1, False), ("q1", 2, True), ("q2", 1, True), ("q2", 2, True)]


def test_resume_counts_but_does_not_repeat_completed_fixed_attribution() -> None:
    fixed = CaseResult(id="fa-a01", category="attribution", question="归因", passed=True,
                       dataset="d", kind="fixed_attribution")
    store = MemoryStore([fixed])
    progress: list[tuple[int, int]] = []
    report = execute(store, 1, [_job("q1")], RunOptions(concurrency=1),
                     lambda result, done, total: progress.append((done, total)))

    assert store.total == 2 and progress == [(2, 2)]
    assert [case.id for case in report.cases] == ["q1", "fa-a01"]
    assert report.as_dict()["groups"]["d/fixed_attribution"]["passed"] == 1


def test_a_case_that_raises_fails_alone() -> None:
    def broken(attempt: int) -> CaseResult:
        raise RuntimeError("model went away")

    store = MemoryStore()
    jobs = [_job("q1"), Job(id="q2", category="metric", question="q2",
                            meta={"dataset": "d", "kind": "data"}, run=broken)]

    report = execute(store, 1, jobs, RunOptions())

    failed = report.cases[1]
    assert not failed.passed and failed.failure_hint == "评测出错"
    assert "RuntimeError: model went away" in failed.detail
    assert store.closed[0] == "done"  # type: ignore[index]


def test_an_interrupted_run_is_marked_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(run_module, "run_jobs", interrupted)
    store = MemoryStore()
    with pytest.raises(KeyboardInterrupt):
        execute(store, 1, [_job("q1")], RunOptions())
    assert store.closed[0] == "cancelled"  # type: ignore[index]


def test_duplicate_case_ids_are_refused() -> None:
    with pytest.raises(ValueError, match="q1"):
        run_module.run_jobs([_job("q1"), _job("q1")], 1, 1)


class _SlowPipeline:
    """Answers after a short pause, recording the order of each session's questions."""

    def __init__(self) -> None:
        self.asked: dict[str, list[str]] = {}
        self.lock = threading.Lock()

    def ask(self, question: str, session_id: str = "default", emit: Any = None,
            mode: str = "query", history: list[HistoryMessage] | None = None) -> AnswerPayload:
        time.sleep(0.01)
        with self.lock:
            self.asked.setdefault(session_id, []).append(question)
        return AnswerPayload(question=question, sql="SELECT 1", summary="ok",
                             table=TableBlock(columns=["a"], rows=[[1]], row_count=1))


def test_multiturn_cases_keep_their_turns_in_order_when_run_at_once(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "execute_reference", lambda settings, sql: [[1]])
    questions = [{"id": f"t{index}", "category": "multiturn", "question": f"第3问-{index}",
                  "turns": [f"第1问-{index}", f"第2问-{index}"], "expected_sql": "SELECT 1"}
                 for index in range(6)]
    pipeline = _SlowPipeline()
    jobs = runner.question_jobs(None, pipeline, questions)  # type: ignore[arg-type]

    report = execute(MemoryStore(), 1, jobs, RunOptions(repeat=2, concurrency=4))

    assert all(case.passed for case in report.cases) and len(report.cases) == 12
    for index in range(6):
        for session_id in (f"eval-t{index}", f"eval-t{index}-2"):
            assert pipeline.asked[session_id] == [f"第1问-{index}", f"第2问-{index}",
                                                  f"第3问-{index}"]


def test_build_jobs_follows_the_mode_and_the_model() -> None:
    settings = Settings(_env_file=None, mysql_eval_password="pw")
    dataset = runner.evaluation_dataset(settings)
    pipeline = _SlowPipeline()
    every = build_jobs(settings, RunOptions(mode="all"), dataset, pipeline)  # type: ignore[arg-type]
    ids = [job.id for job in every]
    assert {"q01", "a01"}.issubset(ids) and "fa-a01" not in ids
    assert any(job.category == "dangerous_sql" for job in every)
    analysis = build_jobs(settings, RunOptions(mode="analysis"), dataset, pipeline)  # type: ignore[arg-type]
    assert analysis and {job.category for job in analysis} == {"analysis"}
    # Without a model only the cases that need none are left.
    security = build_jobs(settings, RunOptions(mode="security"), dataset, None)
    assert {job.category for job in security} == {"dangerous_sql"}
    picked = build_jobs(settings, RunOptions(mode="questions", categories=["metric"], limit=2),
                        dataset, pipeline)  # type: ignore[arg-type]
    assert [job.category for job in picked] == ["metric", "metric"]


@pytest.mark.parametrize("mode", ["all", "analysis", "questions"])
def test_dabench_jobs_use_only_its_reviewed_cases(mode: str) -> None:
    settings = Settings(_env_file=None, mysql_business_db="dabench", mysql_eval_password="pw")
    dataset = runner.evaluation_dataset(settings)
    jobs = build_jobs(settings, RunOptions(mode=mode), dataset, _SlowPipeline())  # type: ignore[arg-type]
    assert len(jobs) == 10
    assert {job.category for job in jobs} == {"attribution", "open"}
    assert all(job.id.startswith("dab-") and job.meta["dataset"] == "dabench" for job in jobs)
    assert not next(job for job in jobs if job.id == "dab-101").meta["scored"]


def test_options_survive_a_resume() -> None:
    options = RunOptions(mode="questions", categories=["metric"], limit=3, history_sql=False,
                         use_plan=False, repeat=3, concurrency=2)
    run = {"mode": "questions", "categories": ["metric"], "options": options.saved(),
           "repeat_count": 3}
    assert RunOptions.from_run(run, concurrency=2) == options


def test_export_keeps_the_fields_of_earlier_result_files() -> None:
    report = runner.EvalReport([CaseResult(id="q1", category="metric", question="q",
                                           passed=True, dataset="d", kind="data")])
    run = {"id": 3, "status": "done", "mode": "all", "dataset": "d", "categories": [],
           "options": {"history_sql": True, "use_plan": True}, "repeat_count": 1,
           "concurrency": 4, "schema_mode": "agentic", "llm_model": "m", "git_commit": "abc",
           "started_at": "2026-11-09 10:00:00", "finished_at": None}
    payload = export_payload(run, report, Settings(_env_file=None))
    earlier = {"mode", "dataset", "categories", "history_sql", "schema_mode", "as_of_date",
               "llm_model", "summary"}
    assert earlier <= set(payload)
    assert {"groups", "unscored", "cases"} <= set(payload["summary"])
    assert payload["summary"]["cases"][0]["id"] == "q1"


class _LockConnection:
    def __init__(self, engine: _LockEngine) -> None:
        self.engine = engine

    def execute(self, statement: Any, params: dict | None = None) -> Any:
        sql = str(statement)
        self.engine.sql.append(sql)
        engine = self.engine

        class _Result:
            rowcount = 0

            def scalar(self) -> int:
                return engine.lock_result

            def __iter__(self) -> Any:
                return iter(engine.running)

        return _Result()


class _LockEngine:
    def __init__(self, lock_result: int, running: list[Any] | None = None) -> None:
        self.lock_result = lock_result
        self.running = running or []
        self.sql: list[str] = []

    @contextmanager
    def connect(self) -> Any:
        yield _LockConnection(self)

    begin = connect


class _Row:
    def __init__(self, **values: Any) -> None:
        self._mapping = values


def test_a_second_run_is_refused_with_the_running_one() -> None:
    settings = Settings(_env_file=None, mysql_eval_password="pw")
    engine = _LockEngine(0, [_Row(id=12, started_at="2026-11-09 10:00:00")])
    store = EvalStore(settings, engine)  # type: ignore[arg-type]
    with pytest.raises(EvalBusy, match="评测 12 正在运行"):
        with store.exclusive():
            pytest.fail("must not run")
    assert not any(sql.startswith("UPDATE") for sql in engine.sql)


def test_the_lock_holder_marks_leftover_runs_failed_and_releases_the_lock() -> None:
    settings = Settings(_env_file=None, mysql_eval_password="pw")
    engine = _LockEngine(1)
    store = EvalStore(settings, engine)  # type: ignore[arg-type]
    with store.exclusive():
        assert any("SET status = 'failed'" in sql for sql in engine.sql)
    assert engine.sql[0] == "SELECT GET_LOCK(:name, 0)"
    assert engine.sql[-1] == "SELECT RELEASE_LOCK(:name)"
    assert store.lock_name == "dataagent_eval:dataagent"


def test_the_local_entry_runs_the_same_command(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location("run_eval", PROJECT_DIR / "eval" / "run_eval.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    received: list[Any] = []
    monkeypatch.setattr(module, "cmd_run", lambda args: received.append(args) or 0)
    assert module.main(["--mode", "questions", "--repeat", "3"]) == 0
    args = received[0]
    assert (args.mode, args.repeat, args.concurrency) == ("questions", 3, 4)
    assert args.output == PROJECT_DIR / "eval" / "results" / "latest.json"


def test_the_eval_tables_match_the_init_script() -> None:
    from app.evaluation.tables import EVAL_CASE_RESULTS_DDL, EVAL_RUNS_DDL
    script = (PROJECT_DIR / "data" / "initdb" / "02_app_schema.sql").read_text(encoding="utf-8")
    assert EVAL_RUNS_DDL in script and EVAL_CASE_RESULTS_DDL in script
