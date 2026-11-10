"""Run evaluation jobs: each case ``repeat`` times, ``concurrency`` at once.

A multi-turn case is one job, so its turns stay in order. Each result is saved as soon
as it is scored; a resumed run skips the (case, attempt) pairs it already has.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from ..agent.pipeline import AskPipeline
from ..config import PROJECT_DIR, Settings
from ..datasets import DatasetManifest
from . import runner
from .runner import CaseResult, EvalReport, Job
from .store import RunStore

MODES = ("questions", "analysis", "routing", "security", "all")


@dataclass(slots=True)
class RunOptions:
    mode: str = "all"
    categories: list[str] = field(default_factory=list)
    limit: int | None = None
    history_sql: bool = True
    use_plan: bool = True
    repeat: int = 1
    concurrency: int = 4

    def saved(self) -> dict[str, Any]:
        """What a resumed run needs to build the same jobs."""
        return {"limit": self.limit, "history_sql": self.history_sql, "use_plan": self.use_plan}

    @classmethod
    def from_run(cls, run: dict[str, Any], concurrency: int) -> RunOptions:
        options = run.get("options") or {}
        return cls(mode=run["mode"], categories=list(run.get("categories") or []),
                   limit=options.get("limit"), history_sql=options.get("history_sql", True),
                   use_plan=options.get("use_plan", True), repeat=run["repeat_count"],
                   concurrency=concurrency)


def git_commit() -> str:
    """The commit the code runs from: git in a checkout, else APP_GIT_COMMIT, else ""."""
    try:
        found = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=PROJECT_DIR,
                               capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        found = None
    if found is not None and found.returncode == 0:
        return found.stdout.strip()
    return os.environ.get("APP_GIT_COMMIT", "")


def build_jobs(settings: Settings, options: RunOptions, dataset: DatasetManifest,
               pipeline: AskPipeline | None) -> list[Job]:
    """The jobs of one mode for this dataset; without a pipeline (no model), only the
    cases that need none. ``all`` leaves out the analysis-only run."""
    mode, limit = options.mode, options.limit
    jobs: list[Job] = []
    if mode in ("questions", "analysis", "all") and pipeline is not None:
        questions = runner.load_dataset_questions(dataset)
        if options.categories:
            questions = [item for item in questions if item["category"] in options.categories]
        if mode == "analysis":
            jobs += runner.analysis_jobs(settings, pipeline, questions, limit)
        else:
            jobs += runner.question_jobs(settings, pipeline, questions, limit, options.history_sql)
    if mode in ("routing", "all") and dataset.eval is not None and dataset.eval.routing is not None:
        routes = runner.load_routing_cases(dataset.eval.routing)
        jobs += runner.routing_jobs(settings, runner.select_cases(routes, dataset.id), limit)
    if mode in ("security", "all"):
        jobs += runner.security_jobs(settings, pipeline, dataset)
    return jobs


def check_unique(jobs: list[Job]) -> None:
    seen: set[str] = set()
    repeated = {job.id for job in jobs if job.id in seen or seen.add(job.id)}  # type: ignore[func-returns-value]
    if repeated:
        raise ValueError(f"这些评测题的 id 重复，结果会互相覆盖：{sorted(repeated)}")


def run_jobs(jobs: list[Job], repeat: int, concurrency: int,
             done: set[tuple[str, int]] | None = None,
             on_result: Callable[[CaseResult], None] = lambda result: None) -> list[CaseResult]:
    """Run every (job, attempt) not yet done, ``concurrency`` at once.

    A job that raises becomes a failed result instead of stopping the run. Results come
    back in job order, then attempt order.
    """
    check_unique(jobs)
    done = done or set()
    todo = [(index, attempt, job) for attempt in range(1, repeat + 1)
            for index, job in enumerate(jobs) if (job.id, attempt) not in done]

    def one(job: Job, attempt: int) -> CaseResult:
        try:
            return job.run(attempt)
        except Exception as exc:  # noqa: BLE001 - recorded on the case, with its type
            return job.failed(attempt, exc)

    finished: list[tuple[int, int, CaseResult]] = []
    with ThreadPoolExecutor(max_workers=max(concurrency, 1)) as pool:
        futures = {pool.submit(one, job, attempt): (index, attempt)
                   for index, attempt, job in todo}
        try:
            for future in as_completed(futures):
                result = future.result()
                on_result(result)
                index, attempt = futures[future]
                finished.append((index, attempt, result))
        except BaseException:
            # Ctrl-C or a failed save: start nothing new; cases already running finish.
            pool.shutdown(wait=True, cancel_futures=True)
            raise
    return [result for _, _, result in sorted(finished, key=lambda item: (item[0], item[1]))]


def ordered(results: list[CaseResult], jobs: list[Job]) -> EvalReport:
    """A report of the results in job order, then attempt order."""
    position = {job.id: index for index, job in enumerate(jobs)}
    report = EvalReport()
    for result in sorted(results, key=lambda item: (position.get(item.id, len(jobs)),
                                                    item.attempt)):
        report.add(result)
    return report


def execute(store: RunStore, run_id: int, jobs: list[Job], options: RunOptions,
            on_result: Callable[[CaseResult, int, int], None] = lambda result, done, total: None
            ) -> EvalReport:
    """Run the jobs of a run that is open in the store, save each result and close the
    run as done, cancelled (Ctrl-C) or failed. Returns all its results, earlier ones too."""
    earlier = store.results(run_id)
    done = {(result.id, result.attempt) for result in earlier}
    # A resumed run may already contain results from the retired fixed-attribution group.
    # Keep those results and count them as completed without scheduling new fixed jobs.
    retired = sum(result.kind == "fixed_attribution" for result in earlier)
    total = len(jobs) * options.repeat + retired
    store.set_total(run_id, total)
    job_ids = {job.id for job in jobs}
    count = sum(case_id in job_ids for case_id, _ in done) + retired

    def saved(result: CaseResult) -> None:
        nonlocal count
        store.save_result(run_id, result)
        count += 1
        on_result(result, count, total)

    try:
        fresh = run_jobs(jobs, options.repeat, options.concurrency, done, saved)
    except KeyboardInterrupt:
        store.finish(run_id, "cancelled", error="手动中断")
        raise
    except Exception as exc:
        store.finish(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
        raise
    report = ordered([*earlier, *fresh], jobs)
    store.finish(run_id, "done", report.summary())
    return report
