"""Evaluation commands.

    python -m app.evaluation run --mode all --repeat 3 --concurrency 4
    python -m app.evaluation run --resume <run_id>
    python -m app.evaluation list
    python -m app.evaluation show <run_id> [--verbose]
    python -m app.evaluation compare <a> <b>
    python -m app.evaluation export <run_id> --output <file>

Runs use this instance's settings and saved model keys. Runs and case results are kept
in the app database (eval_runs, eval_case_results), written with the evaluation account.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from ..agent.pipeline import AskPipeline
from ..agent.scope_model import build_chat_model
from ..config import Settings, get_settings
from ..datasets import DatasetManifest
from ..embedding import build_embedder
from ..rerank import build_reranker
from ..semantic.retriever import SemanticRetriever
from ..vector import build_vector_store
from . import runner
from .report import group_label, pass_counts, print_report
from .retrieval import evaluate_retrieval, print_retrieval
from .run import MODES, RunOptions, build_jobs, execute, git_commit
from .runner import EvalReport, plan_switch
from .store import EvalBusy, EvalStore


def _pipeline(settings: Settings) -> tuple[SemanticRetriever, AskPipeline, Any]:
    store = build_vector_store(settings)
    embedder = build_embedder(settings)
    retriever = SemanticRetriever(settings, store, embedder)
    return retriever, AskPipeline(settings, retriever, source="eval"), embedder


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    print(f"\n结果已写入 {path}")


def export_payload(run: dict[str, Any], report: EvalReport, settings: Settings) -> dict:
    """The JSON file of a run: the fields earlier result files had, and the run's own."""
    options = run.get("options") or {}
    return {
        "run_id": run["id"],
        "status": run["status"],
        "mode": run["mode"],
        "dataset": run["dataset"],
        "categories": run.get("categories") or [],
        "history_sql": options.get("history_sql", True),
        "use_plan": options.get("use_plan", True),
        "repeat": run["repeat_count"],
        "concurrency": run["concurrency"],
        "schema_mode": run["schema_mode"],
        "as_of_date": settings.as_of_date.isoformat(),
        "llm_model": run["llm_model"],
        "git_commit": run["git_commit"],
        "started_at": str(run["started_at"]),
        "finished_at": str(run["finished_at"] or ""),
        "summary": report.as_dict(),
    }


def _run_retrieval(settings: Settings, args: argparse.Namespace, dataset: DatasetManifest) -> int:
    """Retrieval recall: no model, no stored run. Printed, and written with --output."""
    retriever, _, embedder = _pipeline(settings)
    questions = runner.load_dataset_questions(dataset)
    if args.category:
        questions = [item for item in questions if item["category"] in args.category]
    result = evaluate_retrieval(settings, retriever,
                                questions[:args.limit] if args.limit else questions,
                                build_reranker(settings))
    print_retrieval(result)
    if args.output:
        _write(args.output, {
            "mode": "retrieval", "dataset": dataset.id, "categories": args.category or [],
            "as_of_date": settings.as_of_date.isoformat(),
            "embedding_model": getattr(embedder, "model_name", ""), **result,
        })
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.repeat < 1 or args.concurrency < 1:
        print("--repeat 和 --concurrency 至少是 1", file=sys.stderr)
        return 1
    try:
        settings = get_settings()
        dataset = runner.evaluation_dataset(settings)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"业务库 {settings.mysql_business_db}，只跑数据集 {dataset.id} 的评测题")
    # Reference answers, the logs and the results go through the evaluation account.
    problem = runner.eval_account_problem(settings)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    if args.mode == "retrieval":
        try:
            return _run_retrieval(settings, args, dataset)
        except (OSError, ValueError) as exc:
            print(f"无法加载评测题：{exc}", file=sys.stderr)
            return 1

    store = EvalStore(settings)
    try:
        with store.exclusive():
            if args.resume:
                run = store.run(args.resume)
                if run is None:
                    print(f"没有评测 {args.resume}", file=sys.stderr)
                    return 1
                if run["status"] == "done":
                    print(f"评测 {args.resume} 已经完成，不需要续跑", file=sys.stderr)
                    return 1
                if run["dataset"] != dataset.id:
                    print(f"评测 {args.resume} 跑的是 {run['dataset']}，当前实例是 {dataset.id}",
                          file=sys.stderr)
                    return 1
                if run["mode"] not in MODES:
                    print(f"评测 {args.resume} 的模式 {run['mode']} 已移除，历史结果仍可查看",
                          file=sys.stderr)
                    return 1
                options = RunOptions.from_run(run, args.concurrency)
            else:
                options = RunOptions(mode=args.mode, categories=args.category or [],
                                     limit=args.limit, history_sql=not args.no_history_sql,
                                     use_plan=not args.no_plan, repeat=args.repeat,
                                     concurrency=args.concurrency)
            model = build_chat_model(settings)
            if model is None and options.mode in ("questions", "analysis", "routing", "all"):
                print("未配置大模型，无法运行问数或路由评测。请在 project/.env 填写 LLM_API_KEY，"
                      "或在网页上保存模型 key 后在容器里运行。", file=sys.stderr)
                return 1
            pipeline = _pipeline(settings)[1] if model is not None else None
            jobs = build_jobs(settings, options, dataset, pipeline)
            if not jobs:
                print(f"数据集 {dataset.id} 没有符合当前模式和筛选条件的评测题", file=sys.stderr)
                return 1
            if args.resume:
                run_id = args.resume
                store.reopen_run(run_id, options.concurrency)
            else:
                run_id = store.create_run(
                    dataset=dataset.id, mode=options.mode, categories=options.categories,
                    options=options.saved(), repeat=options.repeat,
                    concurrency=options.concurrency, llm_model=model.model if model else "",
                    schema_mode=settings.schema_mode, git_commit=git_commit())
            print(f"评测 {run_id}：{len(jobs)} 道题 × {options.repeat} 次，"
                  f"并发 {options.concurrency}")

            def progress(result: runner.CaseResult, done: int, total: int) -> None:
                mark = "通过" if result.passed else ("不计分" if not result.scored else "未通过")
                print(f"  [{done}/{total}] {result.id}#{result.attempt} {mark} "
                      f"{result.duration_ms / 1000:.1f}s", flush=True)

            try:
                with plan_switch(options.use_plan):
                    report = execute(store, run_id, jobs, options, progress)
            except KeyboardInterrupt:
                print(f"\n评测 {run_id} 已中断，用 --resume {run_id} 继续", file=sys.stderr)
                return 130
    except EvalBusy as exc:
        print(exc, file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"无法加载评测题：{exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(f"评测库读写失败（评测账号 {settings.mysql_eval_user}）："
              f"{getattr(exc, 'orig', None) or exc}。如果是没有权限或缺表，"
              "用管理账号运行一次 python -m app.semantic.cli grant_eval_user", file=sys.stderr)
        return 1

    print(f"\n=== 评测 {run_id} ===")
    print_report(report)
    print(f"\n查看失败题的 SQL 和结论：python -m app.evaluation show {run_id} --verbose")
    if args.output:
        _write(args.output, export_payload(store.run(run_id) or {}, report, settings))
    return 0


def _stored(store: EvalStore, run_id: int) -> tuple[dict[str, Any], EvalReport] | None:
    run = store.run(run_id)
    if run is None:
        print(f"没有评测 {run_id}", file=sys.stderr)
        return None
    report = EvalReport()
    for result in store.results(run_id):
        report.add(result)
    return run, report


def cmd_list(args: argparse.Namespace) -> int:
    store = EvalStore(get_settings())
    runs = store.runs(args.limit)
    if not runs:
        print("还没有评测记录")
    for run in runs:
        groups = (run["summary"] or {}).get("groups", {})
        scores = "，".join(f"{group_label(name)} {item['passed']}/{item['total']}"
                          for name, item in groups.items())
        print(f"{run['id']:>4}  {run['status']:<9} {run['dataset']} {run['mode']} "
              f"×{run['repeat_count']} 并发{run['concurrency']}  {run['done']}/{run['total']}  "
              f"{run['started_at']}  {run['git_commit'] or '-'}  {scores}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    settings = get_settings()
    found = _stored(EvalStore(settings), args.run_id)
    if found is None:
        return 1
    run, report = found
    print(f"评测 {run['id']}：{run['status']}，{run['dataset']} {run['mode']}，"
          f"{run['done']}/{run['total']}，×{run['repeat_count']}，并发 {run['concurrency']}，"
          f"模型 {run['llm_model'] or '-'}，{run['schema_mode']}，提交 {run['git_commit'] or '-'}")
    print(f"开始 {run['started_at']}，结束 {run['finished_at'] or '-'}")
    if run.get("error"):
        print(f"出错：{run['error']}")
    print_report(report, failures=1000, verbose=args.verbose)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    store = EvalStore(get_settings())
    first, second = _stored(store, args.a), _stored(store, args.b)
    if first is None or second is None:
        return 1
    (_, before), (_, after) = first, second
    print(f"评测 {args.a} → 评测 {args.b}")
    old, new = before.summary(), after.summary()
    for group in sorted({*old["groups"], *new["groups"]}):
        line = f"[{group_label(group)}]"
        for name, summary in (("前", old), ("后", new)):
            item = summary["groups"].get(group)
            stats = summary.get("repeat", {}).get(group)
            if item is None:
                line += f" {name}：无"
            elif stats:
                line += (f" {name}：平均 {stats['mean'] * 100:.1f}%"
                         f"（{stats['min'] * 100:.1f}%～{stats['max'] * 100:.1f}%）")
            else:
                line += f" {name}：{item['passed']}/{item['total']}（{item['accuracy'] * 100:.1f}%）"
        print(line)
    old_counts = pass_counts([case for case in before.cases if case.scored])
    new_counts = pass_counts([case for case in after.cases if case.scored])
    both = [case_id for case_id in new_counts if case_id in old_counts]
    rows: dict[str, list[str]] = {"新通过": [], "新失败": [], "有变化": []}
    for case_id in both:
        (old_passed, old_total), (new_passed, new_total) = old_counts[case_id], new_counts[case_id]
        if old_passed / old_total == new_passed / new_total:
            continue
        text = f"{case_id} {old_passed}/{old_total} → {new_passed}/{new_total}"
        if new_passed == new_total:
            rows["新通过"].append(text)
        elif old_passed == old_total:
            rows["新失败"].append(text)
        else:
            rows["有变化"].append(text)
    for name, items in rows.items():
        print(f"{name}（{len(items)}）：" + ("；".join(items) if items else "无"))
    only_old = sorted(set(old_counts) - set(new_counts))
    only_new = sorted(set(new_counts) - set(old_counts))
    if only_old:
        print(f"只在评测 {args.a} 里：{'、'.join(only_old)}")
    if only_new:
        print(f"只在评测 {args.b} 里：{'、'.join(only_new)}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    settings = get_settings()
    found = _stored(EvalStore(settings), args.run_id)
    if found is None:
        return 1
    run, report = found
    payload = export_payload(run, report, settings)
    if args.output:
        _write(args.output, payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m app.evaluation")
    commands = root.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="跑一次评测，逐题写入评测表")
    add_run_arguments(run)

    listing = commands.add_parser("list", help="评测列表和各组分数")
    listing.add_argument("--limit", type=int, default=20)

    show = commands.add_parser("show", help="一次评测的分组分数、不计分的题和失败题")
    show.add_argument("run_id", type=int)
    show.add_argument("--verbose", action="store_true", help="同时列出失败题的 SQL 和结论")

    compare = commands.add_parser("compare", help="两次评测的分数和逐题变化")
    compare.add_argument("a", type=int)
    compare.add_argument("b", type=int)

    export = commands.add_parser("export", help="导出一次评测的 JSON")
    export.add_argument("run_id", type=int)
    export.add_argument("--output", type=Path, default=None)
    return root


def add_run_arguments(run: argparse.ArgumentParser, default_output: Path | None = None) -> None:
    run.add_argument("--mode", choices=[*MODES, "retrieval"], default="all")
    run.add_argument("--no-plan", action="store_true",
                     help="analysis 模式下跳过规划，用单个 Agent 分析，用于和计划流程对比")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--category", action="append", default=None,
                     help="只跑这些类别的问答题，可以写多次，例如 --category consistency")
    run.add_argument("--no-history-sql", action="store_true",
                     help="多轮题的历史里不带上一轮的 SQL，用来和带 SQL 的结果对比")
    run.add_argument("--repeat", type=int, default=1, help="每道题跑几次，用来看波动")
    run.add_argument("--concurrency", type=int, default=4, help="同时跑几道题")
    run.add_argument("--resume", type=int, default=None,
                     help="续跑没跑完的评测，跳过已经入库的题，沿用它的模式和次数")
    run.add_argument("--output", type=Path, default=default_output,
                     help="同时把结果写成 JSON 文件")


COMMANDS = {"run": cmd_run, "list": cmd_list, "show": cmd_show, "compare": cmd_compare,
            "export": cmd_export}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
