"""Backend figures of plan 1.6, read from the archive of each answer."""

from __future__ import annotations

from app.evaluation.runner import Activity, CaseResult, EvalReport, backend_metrics


def _call(kind: str, name: str, duration_ms: int, ok: bool = True, error_type: str = "",
          input_hash: str = "") -> dict:
    return {"kind": kind, "name": name, "duration_ms": duration_ms, "ok": ok,
            "error_type": error_type, "input_hash": input_hash}


def _case(case_id: str, activity: Activity, duration_ms: int, category: str = "metric",
          log_write_ms: int = 0) -> CaseResult:
    return CaseResult(id=case_id, category=category, question="q", passed=True,
                      dataset="d", kind="data", duration_ms=duration_ms, log_id=1,
                      backend_ms=activity.backend_ms, call_errors=activity.call_errors,
                      degraded_calls=activity.degraded_calls, sql_hashes=activity.sql_hashes,
                      embedding_hashes=activity.embedding_hashes,
                      rerank_hashes=activity.rerank_hashes, log_write_ms=log_write_ms)


def test_one_answer_splits_its_time_by_kind_of_call() -> None:
    queries = [{"status": "ok", "error": "", "sql_hash": "s1"}]
    calls = [_call("retrieval", "semantic_retrieve", 80),
             _call("embedding", "text-embedding-v3", 50, input_hash="e1"),
             _call("model", "deepseek-chat", 300),
             _call("tool", "search_semantic", 90),
             _call("embedding", "text-embedding-v3", 30, input_hash="e2"),
             _call("rerank", "qwen3-rerank", 20, input_hash="r1"),
             _call("tool", "run_sql", 110),
             _call("sql", "execute_sql", 100, input_hash="s1"),
             _call("model", "deepseek-chat", 200)]
    activity = Activity.from_archive(queries, calls)
    assert activity.backend_ms == {"model": 500, "sql": 100, "embedding": 80, "rerank": 20,
                                   "retrieval": 80}

    figures = backend_metrics([_case("q1", activity, 750, log_write_ms=12)])
    split = figures["time_split_ms"]
    assert split == {"model": 500.0, "sql": 100.0, "embedding": 80.0, "rerank": 20.0,
                     "other": 50.0, "total": 750.0}
    # Run one after the other, the parts add up to the whole.
    parts = sum(split[name] for name in ("model", "sql", "embedding", "rerank", "other"))
    assert parts == split["total"] and figures["overlapped_cases"] == 0
    assert figures["log_write_ms_avg"] == 12.0 and figures["log_write_ms_max"] == 12


def test_calls_run_at_once_are_counted_but_other_stays_at_zero() -> None:
    activity = Activity.from_archive([], [_call("model", "m", 400), _call("model", "m", 400)])
    figures = backend_metrics([_case("an1", activity, 500, category="analysis")])
    assert figures["time_split_ms"]["other"] == 0.0 and figures["overlapped_cases"] == 1
    assert figures["analysis_max_ms"] == 500


def test_the_same_sql_run_twice_is_one_repeat() -> None:
    first = Activity.from_archive([{"status": "ok", "error": "", "sql_hash": "a"},
                                   {"status": "blocked", "error": "x", "sql_hash": "b"}], [])
    second = Activity.from_archive([{"status": "failed", "error": "1054", "sql_hash": "a"},
                                    {"status": "ok", "error": "", "sql_hash": "c"}], [])
    assert first.sql_hashes == ["a"]
    figures = backend_metrics([_case("q1", first, 10), _case("q2", second, 10)])
    assert figures["repeats"]["sql"] == {"calls": 3, "repeated": 1}


def test_failures_are_counted_by_service_and_kind_and_fallbacks_as_degraded() -> None:
    calls = [_call("model", "m", 10, ok=False, error_type="429"),
             _call("model", "m", 10, ok=False, error_type="timeout"),
             _call("rerank", "r", 10, ok=False, error_type="429"),
             _call("embedding", "e", 10, ok=False, error_type="5xx"),
             _call("sql", "execute_sql", 10, ok=False, error_type="other"),
             _call("tool", "run_sql", 10, ok=False)]
    activity = Activity.from_archive([], calls)
    assert activity.call_errors == {"model:429": 1, "model:timeout": 1, "rerank:429": 1,
                                    "embedding:5xx": 1, "sql:other": 1}
    assert activity.degraded_calls == 2
    long_case = _case("q2", Activity(), 61_000)
    figures = backend_metrics([_case("q1", activity, 100), long_case])
    assert figures["call_errors"]["model:429"] == 1 and figures["degraded_calls"] == 2
    assert figures["long_cases"] == 1


def test_the_figures_are_in_each_group_of_the_report() -> None:
    report = EvalReport([_case("q1", Activity.from_archive([], [_call("model", "m", 5)]), 10)])
    backend = report.summary()["groups"]["d/data"]["metrics"]["backend"]
    assert backend["time_split_ms"]["model"] == 5.0
