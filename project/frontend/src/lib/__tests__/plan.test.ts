import { describe, expect, it } from "vitest";
import { groupResults, stepLabel, stepState } from "../plan";
import { setPlan, startThinking, updatePlanStep } from "../reasoning";
import type { PlanStep, QueryResult } from "../../types";

function query(index: number, stepId: number | null): QueryResult {
  return { index, purpose: `查询${index}`, sql: "", table: null, chart: null, step_id: stepId };
}

const PLAN: PlanStep[] = [
  { id: 1, goal: "整体对比", origin: "plan", status: "done" },
  { id: 2, goal: "按类目拆", origin: "plan", status: "done" },
  { id: 3, goal: "看商品", origin: "supplement", status: "done" },
];

describe("groupResults", () => {
  it("keeps one plain group without a plan", () => {
    const results = [query(1, null), query(2, null)];
    expect(groupResults(results, undefined)).toEqual([{ step: null, results }]);
    expect(groupResults(results, [])).toEqual([{ step: null, results }]);
  });

  it("puts queries under their steps in plan order and drops empty steps", () => {
    const groups = groupResults([query(3, 3), query(1, 1), query(4, 3)], PLAN);
    expect(groups.map((group) => [group.step?.id, group.results.map((item) => item.index)])).toEqual([
      [1, [1]],
      [3, [3, 4]],
    ]);
  });

  it("collects queries of no known step last", () => {
    const groups = groupResults([query(1, 1), query(2, null), query(3, 9)], PLAN);
    expect(groups[groups.length - 1]).toEqual({ step: null, results: [query(2, null), query(3, 9)] });
  });
});

describe("plan progress", () => {
  const pending = PLAN.map((step) => ({ ...step, status: "pending" as const }));

  it("updates one step and records when it started and ended", () => {
    let thinking = setPlan(startThinking(0), pending);
    thinking = updatePlanStep(thinking, { ...pending[1], status: "running" }, 100);
    thinking = updatePlanStep(thinking, { ...pending[1], status: "done" }, 2600);
    expect(thinking.plan?.map((step) => step.status)).toEqual(["pending", "done", "pending"]);
    expect(thinking.plan?.[1]).toMatchObject({ startedAt: 100, endedAt: 2600 });
  });

  it("keeps the times of known steps when a supplement resends the plan", () => {
    let thinking = setPlan(startThinking(0), pending.slice(0, 2));
    thinking = updatePlanStep(thinking, { ...pending[0], status: "running" }, 10);
    thinking = updatePlanStep(thinking, { ...pending[0], status: "done" }, 50);
    thinking = setPlan(thinking, [{ ...PLAN[0] }, { ...PLAN[1] }, { ...pending[2] }]);
    expect(thinking.plan?.[0]).toMatchObject({ status: "done", startedAt: 10, endedAt: 50 });
    expect(thinking.plan?.map((step) => step.id)).toEqual([1, 2, 3]);
  });

  it("ignores a step update without a plan", () => {
    const thinking = startThinking(0);
    expect(updatePlanStep(thinking, PLAN[0], 1)).toBe(thinking);
  });

  it("labels steps that never finished once the answer ended", () => {
    expect(stepLabel("running", true)).toBe("进行中");
    expect(stepLabel("running", false)).toBe("未执行");
    expect(stepState("pending", false)).toBe("unfinished");
    expect(stepLabel("failed", false)).toBe("失败");
  });
});
