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
    if metrics.get("intent"):
        print_intent(metrics["intent"])
    if metrics.get("conclusion", {}).get("answers"):
        print_conclusion(metrics["conclusion"])
    if metrics.get("backend"):
        print_backend(metrics["backend"])


INTENT_LABELS = {"route": "回答方式", "metrics": "指标", "time_range": "时间",
                 "comparison": "对比期", "dimensions": "维度", "filters": "筛选",
                 "result_shape": "结果形式"}


def _share(part: int, total: int) -> str:
    return f"{part}/{total}（{part / total * 100:.0f}%）" if total else "0/0"


def print_conclusion(figures: dict[str, int]) -> None:
    """Conclusions with evidence (plan 2.4)."""
    print(f"  结论：依据都对 {_share(figures['ok'], figures['answers'])}，"
          f"重写后仍有不对的依据 {figures['unchecked']} 题，没读出结构、展示原文 {figures['raw']} 题；"
          f"重写过 {figures['rewritten']} 题；要点 {figures['points']} 条，"
          f"有依据 {figures['points_with_evidence']} 条")


def print_intent(intent: dict[str, Any]) -> None:
    """How questions were understood, and asking back (plan 2.2)."""
    if not intent["read"] and not intent["clarify_expected"]:
        return
    print(f"  意图：识别 {intent['read']} 题，没拿到可用的意图 {intent['no_intent']} 题，"
          f"第一次读不出、重试 {intent['retried']} 题；反问 {intent['clarified']} 题")
    if intent["fields"]:
        parts = "，".join(f"{INTENT_LABELS.get(name, name)} "
                         f"{_share(item['right'], item['total'])}"
                         for name, item in intent["fields"].items())
        print(f"    字段准确率：{parts}")
    print(f"    漏问率（该反问的题没反问）：{_share(intent['clarify_missed'], intent['clarify_expected'])}；"
          f"多问率（不该反问的题反问了）："
          f"{_share(intent['clarify_extra'], intent['clarify_unexpected_total'])}")
    print(f"    SQL 和意图对照：拦下 {intent.get('sql_blocked', 0)} 次"
          f"（{intent.get('sql_blocked_cases', 0)} 题），"
          f"第二次不一致放行并警告 {intent.get('sql_warned', 0)} 次")


SPLIT_LABELS = {"model": "模型", "sql": "SQL", "embedding": "embedding", "rerank": "重排",
                "other": "其他"}
SERVICE_LABELS = {"model": "模型", "sql": "SQL", "embedding": "embedding", "rerank": "重排",
                  "retrieval": "检索"}


def print_backend(backend: dict[str, Any]) -> None:
    """The six backend figures of plan 1.6."""
    split = backend["time_split_ms"]
    if not split:
        return
    print("  后端指标：")
    parts = "，".join(f"{SPLIT_LABELS[name]} {split[name] / 1000:.1f}" for name in SPLIT_LABELS)
    note = (f"（{backend['overlapped_cases']} 题的调用同时进行，各项相加超过总耗时）"
            if backend["overlapped_cases"] else "")
    print(f"    每题耗时拆分（秒，平均）：{parts}，合计 {split['total'] / 1000:.1f}{note}")
    repeats = "，".join(
        f"{SPLIT_LABELS[name]} {item['repeated']}/{item['calls']}"
        f"（{item['repeated'] / item['calls'] * 100:.0f}%）" if item["calls"]
        else f"{SPLIT_LABELS[name]} 0/0"
        for name, item in backend["repeats"].items())
    print(f"    重复请求（重复次数/调用次数）：{repeats}")
    errors = backend["call_errors"]
    if errors:
        text = "，".join(f"{SERVICE_LABELS.get(key.split(':')[0], key.split(':')[0])} "
                         f"{key.split(':')[1]} {count}" for key, count in errors.items())
    else:
        text = "无"
    print(f"    调用失败（服务 类型 次数）：{text}")
    print(f"    降级：{backend['degraded_calls']} 次（embedding 或重排失败后退回）")
    print(f"    长耗时：超过 60 秒 {backend['long_cases']} 题"
          + (f"，分析题最长 {backend['analysis_max_ms'] / 1000:.1f} 秒"
             if backend["analysis_max_ms"] else ""))
    print(f"    写日志耗时：平均 {backend['log_write_ms_avg']:.0f} ms，"
          f"最长 {backend['log_write_ms_max']} ms")


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
