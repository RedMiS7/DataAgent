import type { PlanStep, PlanStepStatus, QueryResult } from "../types";

export interface ResultGroup {
  /** null holds the queries that belong to no step. */
  step: PlanStep | null;
  results: QueryResult[];
}

/** Puts each shown query under its plan step, in plan order. */
export function groupResults(results: QueryResult[], plan: PlanStep[] | undefined): ResultGroup[] {
  if (!plan?.length) return [{ step: null, results }];
  const groups: ResultGroup[] = plan
    .map((step) => ({ step, results: results.filter((result) => result.step_id === step.id) }))
    .filter((group) => group.results.length > 0);
  const known = new Set(plan.map((step) => step.id));
  const rest = results.filter((result) => result.step_id == null || !known.has(result.step_id));
  if (rest.length) groups.push({ step: null, results: rest });
  return groups;
}

const LABELS: Record<PlanStepStatus, string> = {
  pending: "等待",
  running: "进行中",
  done: "完成",
  failed: "失败",
};

/** How a step reads now; one left pending or running when the answer ended never finished. */
export function stepState(status: PlanStepStatus, live: boolean): PlanStepStatus | "unfinished" {
  return !live && (status === "pending" || status === "running") ? "unfinished" : status;
}

export function stepLabel(status: PlanStepStatus, live: boolean): string {
  const state = stepState(status, live);
  return state === "unfinished" ? "未执行" : LABELS[state];
}
