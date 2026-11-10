from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
from contextlib import contextmanager
from datetime import date
from typing import Any

import pytest
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse, ChatUsage
from app.agent import pipeline, planner, session, tools
from app.agent.pipeline import ANALYSIS_ANSWER_RULE, AskPipeline
from app.agent.planner import PlanParseError, PlanStep, parse_steps, results_digest
from app.agent.scope_model import ScriptedChatModel
from app.config import Settings
from app.nl2sql.executor import ExecutionResult
from app.nl2sql.time_parser import parse_time_range
from app.semantic.models import RetrievalBundle

AS_OF = date(2026, 10, 31)
CALL_IDS = itertools.count(1)
OVERALL_SQL = "SELECT 1 AS 本期GMV FROM safe.orders WHERE day >= '2026-09-01'"
CATEGORY_SQL = "SELECT 2 AS 类目 FROM safe.products WHERE day >= '2026-08-01'"
# Steps finish in any order, so the evidence cites rows, not columns, valid for either query.
SUMMARY_JSON = json.dumps({
    "judgement": "整体不错。",
    "points": [{"claim": "GMV 9665990.5", "evidence": [{"query": 1, "rows": [0]}]},
               {"claim": "鞋靴销量 200", "evidence": [{"query": 2, "rows": [0]}]}],
    "advice": "加投放"}, ensure_ascii=False)
# The text the answer shows, with the citations made from the evidence.
SUMMARY = "整体不错。\n- GMV 9665990.5[查询1]\n- 鞋靴销量 200[查询2]\n- 建议：加投放"


def call(name: str, **arguments: str) -> ToolCallBlock:
    return ToolCallBlock(type="tool_call", id=f"plan-call-{next(CALL_IDS)}", name=name,
                         input=json.dumps(arguments, ensure_ascii=False))


def plan(*goals: str) -> list[Any]:
    return [TextBlock(text=json.dumps({"steps": [{"goal": goal} for goal in goals]},
                                      ensure_ascii=False))]


def text(value: str) -> list[Any]:
    return [TextBlock(text=value)]


NO_MORE = plan()


def overall_step(note: str = "本期 GMV 9665990.5") -> list[list[Any]]:
    return [[call("run_sql", sql=OVERALL_SQL, purpose="整体")], text(note)]


def category_step(note: str = "鞋靴销量最高") -> list[list[Any]]:
    return [[call("run_sql", sql=CATEGORY_SQL, purpose="类目拆分")], text(note)]


class RoleModel(ScriptedChatModel):
    """Replies by role: planner, each step, supplement, summary or the single agent.

    Steps run at once and call the model in any order, so each keeps its own script.
    """

    def __init__(self, scripts: dict[str, list[list[Any]]],
                 delay: float | dict[str, float] = 0.0) -> None:
        super().__init__([[]])
        self.scripts = scripts
        self.delay = delay
        self.used = dict.fromkeys(scripts, 0)
        self.order: list[str] = []
        self.running = 0
        self.peak = 0

    @staticmethod
    def role(messages: Any) -> str:
        content = json.dumps(messages, ensure_ascii=False, default=str)
        if "拆成几个查数步骤" in content:
            return "plan"
        if "下面是分析计划和已经查到的结果" in content:
            return "supplement"
        if "请据此给出结论" in content:
            return "summary"
        match = re.search(r"本步任务（第 (\d+) 步）", content)
        return f"step{match.group(1)}" if match else "agent"

    async def _call_api(self, model: str, messages: list | None = None,
                        tools: list[dict] | None = None, tool_choice: Any = None,
                        **kwargs: Any) -> ChatResponse:
        role = self.role(messages)
        self.order.append(role)
        self.calls += 1
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            delay = self.delay.get(role, 0.0) if isinstance(self.delay, dict) else self.delay
            await asyncio.sleep(delay)
        finally:
            self.running -= 1
        script = self.scripts.get(role)
        if not script:
            raise AssertionError(f"没有为 {role} 准备回复")
        index = min(self.used[role], len(script) - 1)
        self.used[role] += 1
        return ChatResponse(content=list(script[index]), is_last=True,
                            usage=ChatUsage(input_tokens=100, output_tokens=10, time=0.0))


class StubRetriever:
    def retrieve_with_connection(self, question: str, conn: object,
                                 trace: Any = None) -> RetrievalBundle:
        return RetrievalBundle()


@pytest.fixture
def analyze(monkeypatch: pytest.MonkeyPatch):
    """Runs a planned analysis through the real agents with a model that replies by role."""
    @contextmanager
    def no_connection(settings: Settings):
        yield None

    saved: dict[str, Any] = {"events": []}

    def fake_execute(settings: Settings, safe: Any) -> ExecutionResult:
        if "2026-08-01" in safe.sql:
            return ExecutionResult(columns=["类目", "销量"], rows=[["鞋靴", 200], ["童装", 150]],
                                   sql=safe.sql)
        return ExecutionResult(columns=["本期GMV"], rows=[[9665990.5]], sql=safe.sql)

    def save_log(conn: object, session_id: str, question: str, rewritten: str, sql: str,
                 *rest: Any, **kwargs: Any) -> int:
        saved["log_sql"] = sql
        saved["tokens"] = rest[-3:-1]
        return 1

    def save_traces(conn: object, log_id: int, records: list[Any]) -> None:
        saved["traces"] = records

    monkeypatch.setattr(pipeline, "app_connection", no_connection)
    monkeypatch.setattr(tools, "execute_sql", fake_execute)
    monkeypatch.setattr(session, "save_query_log", save_log)
    monkeypatch.setattr(session, "save_call_traces", save_traces)
    monkeypatch.setattr(session, "save_query_executions",
                        lambda conn, log_id, records: saved.__setitem__("queries", records))

    def run(scripts: dict[str, list[list[Any]]], delay: float | dict[str, float] = 0.0):
        model = RoleModel(scripts, delay)
        saved["model"] = model
        monkeypatch.setattr(pipeline, "build_chat_model", lambda settings: model)
        settings = Settings(_env_file=None, as_of_date=AS_OF)
        answer = AskPipeline(settings, StubRetriever()).ask(  # type: ignore[arg-type]
            "最近 30 天生意怎么样", "test",
            lambda event, data: saved["events"].append((event, data)), "analysis")
        return answer, saved

    return run


def events_of(saved: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [data for event, data in saved["events"] if event == name]


def by_step(answer: Any) -> dict[int | None, list[str]]:
    """Query purposes per step; steps finish in any order, so query numbers vary."""
    grouped: dict[int | None, list[str]] = {}
    for item in answer.results:
        grouped.setdefault(item.step_id, []).append(item.purpose)
    return grouped


def two_steps(**more: list[list[Any]]) -> dict[str, list[list[Any]]]:
    scripts = {
        "plan": [plan("对比本期和对比期的 GMV", "按类目对比销量")],
        "step1": overall_step(),
        "step2": category_step(),
        "supplement": [NO_MORE],
        "summary": [text(SUMMARY_JSON)],
    }
    scripts.update(more)
    return scripts


# Reading the plan

@pytest.mark.parametrize("reply, goals", [
    ('{"steps": [{"goal": "整体对比"}, {"goal": "按类目拆"}]}', ["整体对比", "按类目拆"]),
    ('```json\n{"steps": [{"goal": " 整体对比 "}]}\n```', ["整体对比"]),
    ('计划如下：{"steps": ["整体对比"]} 完毕', ["整体对比"]),
    ('{"steps": []}', []),
])
def test_parse_steps(reply: str, goals: list[str]) -> None:
    assert parse_steps(reply, 6) == goals


def test_parse_steps_keeps_the_first_steps_and_cuts_long_goals() -> None:
    reply = json.dumps({"steps": [{"goal": "长" * 200}, *[{"goal": f"第{n}步"} for n in range(9)]]})
    goals = parse_steps(reply, planner.MAX_PLAN_STEPS)
    assert len(goals) == planner.MAX_PLAN_STEPS
    assert goals[0] == "长" * planner.GOAL_LIMIT


@pytest.mark.parametrize("reply", [
    "先看整体，再拆类目",
    '{"steps": [{"goal": "整体"}',
    '{"plan": []}',
    '["整体"]',
    '{"steps": [{"goal": ""}]}',
    '{"steps": [{"target": "整体"}]}',
])
def test_parse_steps_rejects_bad_replies(reply: str) -> None:
    with pytest.raises(PlanParseError):
        parse_steps(reply, 6)


def test_the_query_budget_covers_every_step() -> None:
    steps = planner.MAX_PLAN_STEPS + planner.MAX_SUPPLEMENT_STEPS
    assert steps * planner.STEP_MAX_QUERIES <= tools.ANALYSIS_MAX_QUERIES


def test_digest_lists_each_finished_step_with_its_queries() -> None:
    context = tools.AgentRunContext(settings=None, retriever=None)  # type: ignore[arg-type]
    context.executions = [
        ExecutionResult(columns=["GMV"], rows=[[100]], sql="a"),
        ExecutionResult(columns=["类目", "销量"], rows=[[f"类目{n}", n] for n in range(8)], sql="b"),
        ExecutionResult(columns=["渠道"], rows=[], sql="c"),
    ]
    context.purposes = ["整体", "", "渠道"]
    context.step_ids = [1, 2, 2]
    steps = [PlanStep(1, "整体对比", status="done", note="GMV 100"),
             PlanStep(2, "按类目拆", status="done"),
             PlanStep(3, "看广告", status="failed"),
             PlanStep(4, "看物流", status="running")]
    digest = results_digest(context, steps, 5)
    assert digest.split("\n\n") == [
        "第1步：整体对比（已完成）\n[查询1] 整体\n行号 | GMV\n0 | 100\n本步小结：GMV 100",
        "第2步：按类目拆（已完成）\n[查询2] 查询数据\n行号 | 类目 | 销量\n0 | 类目0 | 0\n"
        "1 | 类目1 | 1\n2 | 类目2 | 2\n3 | 类目3 | 3\n4 | 类目4 | 4\n（共 8 行，只列出前 5 行）\n"
        "[查询3] 渠道\n行号 | 渠道\n（没有数据）",
        "第3步：看广告（失败）",
    ]


def test_step_agents_get_the_step_rule_instead_of_the_answer_format() -> None:
    settings = Settings(_env_file=None, as_of_date=AS_OF)
    task = AskPipeline(settings, StubRetriever())._analysis_task(  # type: ignore[arg-type]
        "上个月为什么下滑", [], RetrievalBundle(), parse_time_range("上个月", AS_OF), 7)
    assert planner.STEP_ANSWER_RULE in task.step_prompt
    assert ANALYSIS_ANSWER_RULE not in task.step_prompt
    assert f"最多 {planner.STEP_MAX_QUERIES} 条查询" in task.step_prompt
    assert "对比期（上一个周期）：[2026-08-01, 2026-09-01)" in task.period
    assert task.time_note.startswith("[系统信息] 时间范围 上个月")
    assert task.step_max_iters == 7


def test_the_planner_is_told_that_steps_run_at_once() -> None:
    assert "同时执行" in planner.PLAN_PROMPT


# The planned analysis

def test_a_plan_runs_each_step_then_summarizes(analyze) -> None:
    answer, saved = analyze(two_steps())
    assert answer.status == "ok"
    assert answer.mode == "analysis"
    assert answer.summary == SUMMARY
    assert answer.summary_source == "model"
    assert answer.warnings == []
    assert [step.model_dump() for step in answer.plan] == [
        {"id": 1, "goal": "对比本期和对比期的 GMV", "origin": "plan", "status": "done"},
        {"id": 2, "goal": "按类目对比销量", "origin": "plan", "status": "done"},
    ]
    assert by_step(answer) == {1: ["整体"], 2: ["类目拆分"]}
    assert saved["log_sql"] == ""
    assert saved["model"].calls == 7


def test_every_agent_of_a_plan_is_traced(analyze) -> None:
    answer, saved = analyze(two_steps())
    models = [item for item in saved["traces"] if item.kind == "model"]
    assert sorted({item.agent for item in models}) == [
        "planner", "router", "step_1", "step_2", "summary", "supplement"]
    assert sorted((item.agent, item.name) for item in saved["traces"] if item.kind == "tool") == [
        ("step_1", "run_sql"), ("step_2", "run_sql")]
    assert answer.usage is not None
    # The router has a model of its own; the rest share the scripted one.
    assert answer.usage.model_calls == len(models) == saved["model"].calls + 1 == 8
    assert saved["tokens"] == (700, 70)
    assert (answer.usage.input_tokens, answer.usage.output_tokens) == (700, 70)
    starts = [item.started_ms for item in saved["traces"]]
    assert starts == sorted(starts)


def test_steps_run_at_the_same_time(analyze) -> None:
    scripts = two_steps(plan=[plan("整体", "类目", "渠道")], step3=overall_step())
    answer, saved = analyze(scripts, delay=0.05)
    assert answer.status == "ok"
    assert saved["model"].peak == 3


def test_a_plan_reports_its_progress(analyze) -> None:
    _, saved = analyze(two_steps())
    assert events_of(saved, "plan") == [{"steps": [
        {"id": 1, "goal": "对比本期和对比期的 GMV", "origin": "plan", "status": "pending"},
        {"id": 2, "goal": "按类目对比销量", "origin": "plan", "status": "pending"},
    ]}]
    updates = [(item["id"], item["status"]) for item in events_of(saved, "step")]
    assert sorted(updates) == [(1, "done"), (1, "running"), (2, "done"), (2, "running")]
    for step_id in (1, 2):
        assert updates.index((step_id, "running")) < updates.index((step_id, "done"))
    lines = events_of(saved, "status")
    steps = [item["step"] for item in lines]
    assert steps.index("planning") < steps.index("supplementing") < steps.index("summarizing")
    queries = sorted(item["message"] for item in lines if item["step"].startswith("query_"))
    assert len(queries) == 2
    assert re.fullmatch(r"第1步 · 查询 [12]：整体，返回 1 行", queries[0])
    assert re.fullmatch(r"第2步 · 查询 [12]：类目拆分，返回 2 行", queries[1])
    answer = events_of(saved, "answer")[0]
    assert [item["status"] for item in answer["plan"]] == ["done", "done"]


def test_first_round_steps_see_no_results_and_the_supplement_sees_them(
        analyze, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs: dict[int, str] = {}
    original = planner._step_input

    def capture(task: Any, context: Any, step: PlanStep, steps: Any) -> str:
        inputs[step.id] = original(task, context, step, steps)
        return inputs[step.id]

    monkeypatch.setattr(planner, "_step_input", capture)
    analyze(two_steps(supplement=[plan("看鞋靴类目里各商品的销量")],
                      step3=category_step("鞋靴里 A 款最多")))
    assert "本步任务（第 1 步）：对比本期和对比期的 GMV" in inputs[1]
    assert "前面步骤查到的结果：\n（无）" in inputs[1]
    assert "前面步骤查到的结果：\n（无）" in inputs[2]
    assert "行号 | 本期GMV\n0 | 9665990.5\n本步小结：本期 GMV 9665990.5" in inputs[3]
    assert "本步小结：鞋靴销量最高" in inputs[3]
    assert "[系统信息] 时间范围" in inputs[3]


def test_a_supplement_adds_one_step_after_the_plan(analyze) -> None:
    answer, saved = analyze(two_steps(
        supplement=[plan("看鞋靴类目里各商品的销量", "再看渠道")],
        step3=[[call("run_sql", sql=CATEGORY_SQL, purpose="鞋靴商品")], text("鞋靴里 A 款最多")],
    ))
    assert answer.status == "ok"
    assert [(step.id, step.origin, step.status) for step in answer.plan] == [
        (1, "plan", "done"), (2, "plan", "done"), (3, "supplement", "done")]
    plans = events_of(saved, "plan")
    assert len(plans) == 2 and len(plans[1]["steps"]) == 3


def test_an_unreadable_supplement_is_skipped(analyze, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app"):
        answer, _ = analyze(two_steps(supplement=[text("还要看广告")]))
    assert answer.summary == SUMMARY
    assert len(answer.plan) == 2
    assert any("补查结果无法解析" in item.getMessage() and "还要看广告" in item.getMessage()
               for item in caplog.records)


def test_a_step_without_queries_fails_and_the_rest_still_run(analyze) -> None:
    answer, saved = analyze(two_steps(
        plan=[plan("看广告投放", "按类目对比销量")],
        step1=[text("没有广告数据")],
        summary=[text("鞋靴领先。\n- 鞋靴销量 200 [查询1]\n- 广告数据缺失\n- 建议：补充投放")],
    ))
    # The conclusion stands, but the card says which step has no data.
    assert answer.status == "partial"
    assert answer.summary_source == "model"
    assert answer.failure is not None and answer.failure.kind == "step_failed"
    assert "看广告投放" in answer.failure.message
    assert [step.status for step in answer.plan] == ["failed", "done"]
    assert by_step(answer) == {2: ["类目拆分"]}
    assert ("step", {"id": 1, "goal": "看广告投放", "origin": "plan", "status": "failed"}) \
        in saved["events"]


def test_a_step_that_raises_is_logged_and_the_others_finish(
        analyze, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    original = planner._step_input

    def fail_first(task: Any, context: Any, step: PlanStep, steps: Any) -> str:
        if step.id == 1:
            raise RuntimeError("步骤坏了")
        return original(task, context, step, steps)

    monkeypatch.setattr(planner, "_step_input", fail_first)
    with caplog.at_level(logging.INFO, logger="app"):
        answer, _ = analyze(two_steps(
            summary=[text("鞋靴领先。\n- 鞋靴销量 200 [查询1]\n- 建议：补充投放")]))
    assert [step.status for step in answer.plan] == ["failed", "done"]
    failure = next(item for item in caplog.records if "第 1 步执行失败" in item.getMessage())
    assert failure.exc_info is not None


def test_no_query_in_any_step_is_an_error(analyze) -> None:
    answer, saved = analyze({"plan": [plan("整体对比", "类目拆分")],
                             "step1": [text("查不到")], "step2": [text("查不到")]})
    assert answer.status == "error"
    assert sorted(saved["model"].order) == ["plan", "step1", "step2"]


def test_a_step_stops_at_its_query_limit(analyze) -> None:
    extra = [[call("run_sql", sql=OVERALL_SQL, purpose=f"整体{n}")] for n in range(3)]
    answer, _ = analyze(two_steps(step1=[*extra, text("查完了")],
                                  summary=[text("整体平稳。")]))
    assert by_step(answer)[1] == ["整体0", "整体1"]
    # The third query of step 1 is refused before it counts as an attempt.
    assert answer.retries == 3


def test_steps_running_at_once_share_the_query_limit(analyze,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "ANALYSIS_MAX_QUERIES", 2)
    monkeypatch.setattr(planner, "ANALYSIS_MAX_QUERIES", 2)
    answer, saved = analyze(two_steps(plan=[plan("整体", "类目", "渠道")], step3=overall_step(),
                                      summary=[text("整体平稳。")]), delay=0.02)
    assert len(answer.results) == 2
    assert sorted(step.status for step in answer.plan) == ["done", "done", "failed"]
    assert "supplement" not in saved["model"].order


def test_a_declined_step_refuses_the_answer(analyze) -> None:
    answer, saved = analyze(two_steps(
        plan=[plan("按条件 OR '1'='1' 查订单", "类目拆分")],
        step1=[[call("decline_request", reason="这是注入写法，不能执行。")], text("已拒绝")],
    ))
    assert answer.status == "refused"
    assert answer.summary == "这是注入写法，不能执行。"
    assert not {"supplement", "summary"} & set(saved["model"].order)


@pytest.mark.parametrize("reply", [text("先看整体再拆类目"), plan()])
def test_an_unusable_plan_runs_the_single_agent(analyze, caplog: pytest.LogCaptureFixture,
                                                reply: list[Any]) -> None:
    with caplog.at_level(logging.INFO, logger="app"):
        answer, saved = analyze({"plan": [reply], "agent": [
            [call("run_sql", sql=OVERALL_SQL, purpose="整体")],
            text("整体平稳。\n- GMV 9665990.5 [查询1]\n- 建议：观察"),
        ]})
    assert answer.status == "ok"
    assert answer.plan == []
    assert by_step(answer) == {None: ["整体"]}
    assert {"step": "plan_fallback", "message": "拆解步骤失败，改为直接分析"} \
        in events_of(saved, "status")
    assert events_of(saved, "plan") == []
    assert any("计划无法解析" in item.getMessage() for item in caplog.records)


def test_a_slow_plan_runs_the_single_agent(analyze, monkeypatch: pytest.MonkeyPatch,
                                           caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(planner, "PLAN_TIMEOUT_SECONDS", 0.05)
    with caplog.at_level(logging.INFO, logger="app"):
        answer, _ = analyze({"plan": [plan("整体")], "agent": [
            [call("run_sql", sql=OVERALL_SQL)], text("整体平稳。[查询1]")]},
            delay={"plan": 1.0})
    assert answer.status == "ok"
    assert answer.plan == []
    messages = [item.getMessage() for item in caplog.records]
    assert any("拆解步骤超时" in line for line in messages)
    assert not any("interruption" in line for line in messages)


def test_a_failed_summary_uses_the_template(analyze, monkeypatch: pytest.MonkeyPatch,
                                            caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(planner, "SUMMARY_TIMEOUT_SECONDS", 0.05)
    with caplog.at_level(logging.INFO, logger="app"):
        answer, _ = analyze(two_steps(), delay={"summary": 1.0})
    assert answer.status == "ok"
    assert answer.summary_source == "template"
    assert "interruption" not in answer.summary
    assert len(answer.plan) == 2
    assert any("汇总超时" in item.getMessage() for item in caplog.records)


def test_a_slow_supplement_is_skipped(analyze, monkeypatch: pytest.MonkeyPatch,
                                      caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(planner, "SUPPLEMENT_TIMEOUT_SECONDS", 0.05)
    with caplog.at_level(logging.INFO, logger="app"):
        answer, _ = analyze(two_steps(supplement=[plan("再看渠道")]), delay={"supplement": 1.0})
    assert len(answer.plan) == 2
    assert any("补查判断超时" in item.getMessage() for item in caplog.records)


def test_the_summary_is_kept_as_written(analyze) -> None:
    # 6.3% is in no query result; the conclusion is no longer checked by code.
    conclusion = "整体增长。\n- GMV 增长 6.3% [查询1]"
    answer, _ = analyze(two_steps(summary=[text(conclusion)]))
    assert answer.summary == conclusion
    assert answer.summary_source == "model"
    assert answer.warnings == []


def test_only_the_cited_step_is_shown(analyze) -> None:
    # Query numbers depend on which step finishes first; either way one query is uncited.
    only_first = json.dumps({"judgement": "整体不错。", "points": [
        {"claim": "GMV 9665990.5", "evidence": [{"query": 1, "rows": [0]}]}]}, ensure_ascii=False)
    answer, _ = analyze(two_steps(summary=[text(only_first)]))
    assert answer.warnings == []
    assert [item.index for item in answer.results] == [1]
    assert answer.conclusion is not None and answer.conclusion.checked


# Eval scoring

def _runner() -> Any:
    from app.evaluation import runner
    return runner


def _answer(**fields: Any) -> Any:
    from app.api.schemas import AnswerPayload, PlanStepInfo, QueryResult
    base: dict[str, Any] = {"question": "q", "status": "ok", "summary_source": "model",
                            "results": [QueryResult(index=1, purpose="渠道 GMV 对比")]}
    base.update(fields)
    base["plan"] = [PlanStepInfo(id=n, goal=goal, status="done")
                    for n, goal in enumerate(base.get("plan", []), start=1)]
    return AnswerPayload(**base)


def test_eval_counts_dimensions_from_plan_goals_and_query_purposes() -> None:
    runner = _runner()
    answer = _answer(plan=["对比本期和对比期的整体 GMV", "按品类拆分销量"])
    assert runner.covered_dimensions(answer, ["整体", "类目", "渠道", "物流"]) == [
        "整体", "类目", "渠道"]


def test_eval_scores_an_analysis() -> None:
    runner = _runner()
    question = {"expected_dimensions": ["整体", "渠道"]}
    passed, detail = runner.score_analysis(_answer(plan=["整体对比"]), question)
    assert passed and "dimensions=2/2" in detail and "plan=1/1" in detail
    passed, detail = runner.score_analysis(_answer(), question)
    assert not passed and "缺整体" in detail
    warned = _answer(plan=["整体对比"], warnings=["这些数字未能在查询结果中核对：6.3%"])
    # Unverified figures are no longer scored (plan 1.4).
    passed, _ = runner.score_analysis(warned, question)
    assert passed
