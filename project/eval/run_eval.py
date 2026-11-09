from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "backend"))
sys.path.insert(0, str(PROJECT_DIR))

from app.agent.pipeline import AskPipeline  # noqa: E402
from app.agent.scope_model import build_chat_model  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.embedding import build_embedder  # noqa: E402
from app.rerank import build_reranker  # noqa: E402
from app.semantic.retriever import SemanticRetriever  # noqa: E402
from app.vector import build_vector_store  # noqa: E402

from eval.retrieval_eval import evaluate_retrieval, print_retrieval  # noqa: E402
from eval.runner import (  # noqa: E402
    EvalReport,
    dataset_for,
    eval_account_problem,
    evaluate_analysis,
    evaluate_attribution,
    evaluate_questions,
    evaluate_routing,
    evaluate_security,
    load_questions,
    load_routing_cases,
    merge_reports,
    select_cases,
    summarize_cases,
)

RESULT_DIR = PROJECT_DIR / "eval" / "results"


GROUP_LABELS = {
    "data": "数据题",
    "routing": "路由题",
    "rule": "规则题",
    "behavior": "行为题（拒答、直接回答、注入）",
    "fixed_attribution": "固定归因流程",
}


def _print_report(name: str, report: EvalReport) -> None:
    """Each "dataset/kind" group on its own; there is no total over the groups."""
    print(f"\n=== {name} ===")
    if not report.cases:
        print("当前数据集没有这类题")
    for group, cases in report.groups().items():
        summary = summarize_cases(cases)
        dataset, kind = group.split("/", 1)
        print(f"[{dataset} · {GROUP_LABELS.get(kind, kind)}] "
              f"{summary['passed']}/{summary['total']}，准确率 {summary['accuracy'] * 100:.1f}%")
        for category, bucket in sorted(summary["by_category"].items()):
            rate = bucket["passed"] / bucket["total"] * 100 if bucket["total"] else 0.0
            print(f"  {category}: {bucket['passed']}/{bucket['total']} ({rate:.1f}%)")
        _print_metrics(summary["metrics"])
        failures = [case for case in cases if not case.passed]
        if failures:
            print("  未通过：")
            for case in failures[:20]:
                hint = f"（{case.failure_hint}）" if case.failure_hint else ""
                print(f"    - {case.id} [{case.category}] {case.question[:40]} -> {case.detail}{hint}")
    unscored = report.unscored()
    if unscored:
        print("不计分（存疑题，照常运行和记录）：")
        for case in unscored:
            print(f"    - {case.id} [{case.category}] {'通过' if case.passed else '未通过'}"
                  f" -> {case.detail}")


def _print_metrics(metrics: dict) -> None:
    if metrics["questions_with_sql"]:
        print(f"  SQL 无报错的题：{metrics['sql_clean_rate'] * 100:.1f}%"
              f"（{metrics['questions_with_sql']} 题执行过 SQL），"
              f"SQL {metrics['sql_attempts']} 条，失败 {metrics['sql_failures']} 条，"
              f"其中字段不存在 {metrics['field_errors']} 次；"
              f"describe_table {metrics['describe_calls']} 次")
        print(f"  search_semantic {metrics.get('search_calls', 0)} 次；"
              f"用了没检索过的表被打回 {metrics.get('unseen_rejections', 0)} 次")
    if metrics.get("cache_hit_rate") is not None:
        print(f"  平均输入 token {metrics['input_tokens_avg']:.0f}，"
              f"其中缓存命中 {metrics['cached_input_tokens_avg']:.0f}，"
              f"命中率 {metrics['cache_hit_rate'] * 100:.1f}%")
    if metrics["loose_total"]:
        print(f"  宽松比对（允许多出的列）：{metrics['loose_passed']}/{metrics['loose_total']}")
    if metrics["failure_hints"]:
        hints = "，".join(f"{name} {count}" for name, count in
                         sorted(metrics["failure_hints"].items(), key=lambda item: -item[1]))
        print(f"  失败现象：{hints}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run DataAgent evaluation")
    parser.add_argument("--mode",
                        choices=["questions", "analysis", "routing", "security", "attribution",
                                 "retrieval", "all"],
                        default="all")
    parser.add_argument("--no-plan", action="store_true",
                        help="analysis 模式下跳过规划，用单个 Agent 分析，用于和计划流程对比")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--category", action="append", default=None,
                        help="只跑这些类别的问答题，可以写多次，例如 --category consistency")
    parser.add_argument("--no-history-sql", action="store_true",
                        help="多轮题的历史里不带上一轮的 SQL，用来和带 SQL 的结果对比")
    parser.add_argument("--output", type=Path, default=RESULT_DIR / "latest.json")
    args = parser.parse_args()

    settings = get_settings()
    try:
        dataset = dataset_for(settings)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"业务库 {settings.mysql_business_db}，只跑数据集 {dataset} 的评测题")
    # Reference answers and the semantic layer are read with the evaluation account.
    problem = eval_account_problem(settings)
    if problem and args.mode in ("questions", "retrieval", "all"):
        print(problem, file=sys.stderr)
        return 1
    model = build_chat_model(settings)
    if model is None and args.mode in ("questions", "analysis", "routing", "all"):
        print("未配置大模型，无法运行问数或路由评测。请在 project/.env 填写 LLM_API_KEY。",
              file=sys.stderr)
        return 1

    store = build_vector_store(settings)
    embedder = build_embedder(settings)
    retriever = SemanticRetriever(settings, store, embedder)
    pipeline = AskPipeline(settings, retriever, source="eval")

    questions = select_cases(load_questions(), dataset)
    if args.category:
        questions = [item for item in questions if item["category"] in args.category]
    if args.mode == "retrieval":
        # Not part of "all": it calls no model and is run on its own while tuning retrieval.
        result = evaluate_retrieval(settings, retriever, questions[:args.limit]
                                    if args.limit else questions, build_reranker(settings))
        print_retrieval(result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({
            "mode": "retrieval",
            "dataset": dataset,
            "categories": args.category or [],
            "as_of_date": settings.as_of_date.isoformat(),
            "embedding_model": getattr(embedder, "model_name", ""),
            **result,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {args.output}")
        return 0

    reports: list[EvalReport] = []
    if args.mode in ("questions", "all"):
        report = evaluate_questions(settings, pipeline, questions, args.limit,
                                    history_sql=not args.no_history_sql)
        _print_report("问答评测", report)
        reports.append(report)
    if args.mode == "analysis":
        report = evaluate_analysis(pipeline, questions, args.limit, use_plan=not args.no_plan)
        _print_report("分析评测" + ("（不用计划）" if args.no_plan else ""), report)
        durations = sorted(case.duration_ms for case in report.cases)
        if durations:
            count = len(report.cases)
            print(f"  耗时：平均 {sum(durations) / count / 1000:.1f} 秒，"
                  f"最长 {durations[-1] / 1000:.1f} 秒")
            print(f"  模型调用：平均 {sum(c.model_calls for c in report.cases) / count:.1f} 次，"
                  f"tokens 平均 {sum(c.input_tokens for c in report.cases) / count:.0f}"
                  f"/{sum(c.output_tokens for c in report.cases) / count:.0f}")
        reports.append(report)
    if args.mode in ("routing", "all"):
        report = evaluate_routing(settings, select_cases(load_routing_cases(), dataset),
                                  args.limit)
        _print_report("路由评测", report)
        reports.append(report)
    if args.mode in ("attribution", "all"):
        report = evaluate_attribution(settings, questions, args.limit)
        _print_report("归因评测", report)
        reports.append(report)
    if args.mode in ("security", "all"):
        report = evaluate_security(settings, pipeline if model else None, dataset)
        _print_report("安全评测", report)
        reports.append(report)

    merged = merge_reports(reports)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "mode": args.mode,
        "dataset": dataset,
        "categories": args.category or [],
        "history_sql": not args.no_history_sql,
        "schema_mode": settings.schema_mode,
        "as_of_date": settings.as_of_date.isoformat(),
        "llm_model": model.model if model else "",
        "summary": merged.as_dict(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())