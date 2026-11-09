"""Print evaluation scores: each "dataset/kind" group on its own, with no total."""

from __future__ import annotations

from typing import Any

from .runner import CaseResult, EvalReport, summarize_cases

GROUP_LABELS = {
    "data": "数据题",
    "routing": "路由题",
    "rule": "规则题",
    "behavior": "行为题（拒答、直接回答、注入）",
    "fixed_attribution": "固定归因流程",
}


def group_label(group: str) -> str:
    dataset, _, kind = group.partition("/")
    return f"{dataset} · {GROUP_LABELS.get(kind, kind)}"


def _percent(value: float | None) -> str:
    return f"{(value or 0) * 100:.1f}%"


def print_metrics(metrics: dict[str, Any]) -> None:
    if metrics["questions_with_sql"]:
        print(f"  SQL 无报错的题：{_percent(metrics['sql_clean_rate'])}"
              f"（{metrics['questions_with_sql']} 题执行过 SQL），"
              f"SQL {metrics['sql_attempts']} 条，失败 {metrics['sql_failures']} 条，"
              f"其中字段不存在 {metrics['field_errors']} 次；"
              f"describe_table {metrics['describe_calls']} 次")
        print(f"  search_semantic {metrics.get('search_calls', 0)} 次；"
              f"用了没检索过的表被打回 {metrics.get('unseen_rejections', 0)} 次")
    if metrics.get("cache_hit_rate") is not None:
        print(f"  平均输入 token {metrics['input_tokens_avg']:.0f}，"
              f"其中缓存命中 {metrics['cached_input_tokens_avg']:.0f}，"
              f"命中率 {_percent(metrics['cache_hit_rate'])}")
    if metrics.get("duration_ms_avg"):
        print(f"  平均耗时 {metrics['duration_ms_avg'] / 1000:.1f} 秒")
    if metrics["loose_total"]:
        print(f"  比对行的题 {metrics['loose_total']} 道：宽松比对（计分）通过 "
              f"{metrics['loose_passed']}，严格比对（列也一致，参考）通过 "
              f"{metrics.get('strict_passed', 0)}")
    if metrics["failure_hints"]:
        hints = "，".join(f"{name} {count}" for name, count in
                         sorted(metrics["failure_hints"].items(), key=lambda item: -item[1]))
        print(f"  失败现象：{hints}")


def pass_counts(cases: list[CaseResult]) -> dict[str, tuple[int, int]]:
    """For each case id: attempts passed and attempts run."""
    counts: dict[str, tuple[int, int]] = {}
    for case in cases:
        passed, total = counts.get(case.id, (0, 0))
        counts[case.id] = (passed + case.passed, total + 1)
    return counts


def print_report(report: EvalReport, *, failures: int = 20, verbose: bool = False) -> None:
    summary = report.summary()
    if not report.cases:
        print("没有评测题")
    repeat = summary.get("repeat", {})
    for group, cases in report.groups().items():
        scores = summarize_cases(cases)
        line = f"[{group_label(group)}] {scores['passed']}/{scores['total']}，准确率 " \
               f"{_percent(scores['accuracy'])}"
        if group in repeat:
            stats = repeat[group]
            line += (f"；{stats['attempts']} 次：平均 {_percent(stats['mean'])}"
                     f"（{_percent(stats['min'])}～{_percent(stats['max'])}），"
                     f"每次都通过 {_percent(stats['pass_all'])}")
        print(line)
        for category, bucket in sorted(scores["by_category"].items()):
            rate = bucket["passed"] / bucket["total"] if bucket["total"] else 0.0
            print(f"  {category}: {bucket['passed']}/{bucket['total']} ({_percent(rate)})")
        print_metrics(scores["metrics"])
        counts = pass_counts(cases)
        failed = [case for case in cases if not case.passed]
        shown: set[str] = set()
        if failed:
            print("  未通过：")
        for case in failed:
            if case.id in shown or len(shown) >= failures:
                continue
            shown.add(case.id)
            passed, total = counts[case.id]
            times = f" {passed}/{total} 次通过" if total > 1 else ""
            hint = f"（{case.failure_hint}）" if case.failure_hint else ""
            log = f" log={case.log_id}" if case.log_id else ""
            print(f"    - {case.id} [{case.category}]{times} {case.question[:40]} -> "
                  f"{case.detail}{hint}{log}")
            if verbose:
                if case.sql:
                    print(f"        SQL：{' '.join(case.sql.split())[:400]}")
                if case.summary:
                    print(f"        结论（{case.summary_source or '-'}）："
                          f"{' '.join(case.summary.split())[:300]}")
    unscored = report.unscored()
    if unscored:
        print("不计分（照常运行和记录）：")
        seen: set[str] = set()
        for case in unscored:
            if case.id in seen:
                continue
            seen.add(case.id)
            print(f"    - {case.id} [{case.category}] {case.unscored_reason or '不计分'}，"
                  f"{'通过' if case.passed else '未通过'} -> {case.detail}")
