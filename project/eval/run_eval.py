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
from app.semantic.retriever import SemanticRetriever  # noqa: E402
from app.vector import build_vector_store  # noqa: E402

from eval.runner import (  # noqa: E402
    EvalReport,
    evaluate_attribution,
    evaluate_questions,
    evaluate_security,
    load_questions,
    merge_reports,
)

RESULT_DIR = PROJECT_DIR / "eval" / "results"


def _print_report(name: str, report: EvalReport) -> None:
    accuracy = report.passed / report.total * 100 if report.total else 0.0
    print(f"\n=== {name} ===")
    print(f"总计 {report.passed}/{report.total}，准确率 {accuracy:.1f}%")
    for category, bucket in sorted(report.by_category.items()):
        rate = bucket["passed"] / bucket["total"] * 100 if bucket["total"] else 0.0
        print(f"  {category}: {bucket['passed']}/{bucket['total']} ({rate:.1f}%)")
    failures = [case for case in report.cases if not case.passed]
    if failures:
        print("  未通过：")
        for case in failures[:20]:
            print(f"    - {case.id} [{case.category}] {case.question[:40]} -> {case.detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run DataAgent evaluation")
    parser.add_argument("--mode", choices=["questions", "security", "attribution", "all"],
                        default="all")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output", type=Path, default=RESULT_DIR / "latest.json")
    args = parser.parse_args()

    settings = get_settings()
    model = build_chat_model(settings)
    if model is None and args.mode in ("questions", "all"):
        print("未配置大模型，无法运行问数评测。请在 project/.env 填写 LLM_API_KEY。",
              file=sys.stderr)
        return 1

    store = build_vector_store(settings)
    embedder = build_embedder(settings)
    retriever = SemanticRetriever(settings, store, embedder)
    pipeline = AskPipeline(settings, retriever)

    questions = load_questions()
    reports: list[EvalReport] = []
    if args.mode in ("questions", "all"):
        report = evaluate_questions(settings, pipeline, questions, args.limit)
        _print_report("问答评测", report)
        reports.append(report)
    if args.mode in ("attribution", "all"):
        report = evaluate_attribution(settings, questions, args.limit)
        _print_report("归因评测", report)
        reports.append(report)
    if args.mode in ("security", "all"):
        report = evaluate_security(settings, pipeline if model else None)
        _print_report("安全评测", report)
        reports.append(report)

    merged = merge_reports(reports)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "mode": args.mode,
        "as_of_date": settings.as_of_date.isoformat(),
        "llm_model": model.model if model else "",
        "summary": merged.as_dict(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())