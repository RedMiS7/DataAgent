import type { PlanStep } from "../types";

/** A plan step with the client times it started and finished. */
export interface PlanStepView extends PlanStep {
  startedAt?: number;
  endedAt?: number;
}

export interface ThinkingStep {
  step: string;
  message: string;
  startedAt: number;
  endedAt?: number;
}

export interface Thinking {
  steps: ThinkingStep[];
  /** Model reasoning text. Stays empty until the backend sends `reasoning` events. */
  text: string;
  /** The analysis plan, when the backend made one. */
  plan?: PlanStepView[];
  startedAt: number;
  endedAt?: number;
}

export function startThinking(now: number): Thinking {
  return { steps: [], text: "", startedAt: now };
}

export function addStep(thinking: Thinking, step: string, message: string, now: number): Thinking {
  const steps = thinking.steps.slice();
  const last = steps[steps.length - 1];
  if (last && last.step === step) {
    steps[steps.length - 1] = { ...last, message };
    return { ...thinking, steps };
  }
  if (last) steps[steps.length - 1] = { ...last, endedAt: now };
  steps.push({ step, message, startedAt: now });
  return { ...thinking, steps };
}

export function addReasoning(thinking: Thinking, delta: string): Thinking {
  return { ...thinking, text: thinking.text + delta };
}

/** A supplement resends the whole plan; steps already known keep their times. */
export function setPlan(thinking: Thinking, plan: PlanStep[]): Thinking {
  const known = new Map((thinking.plan ?? []).map((step) => [step.id, step]));
  return { ...thinking, plan: plan.map((step) => ({ ...known.get(step.id), ...step })) };
}

export function updatePlanStep(thinking: Thinking, update: PlanStep, now: number): Thinking {
  if (!thinking.plan) return thinking;
  const plan = thinking.plan.map((step) => {
    if (step.id !== update.id) return step;
    const next: PlanStepView = { ...step, ...update };
    if (update.status === "running") next.startedAt = now;
    if (update.status === "done" || update.status === "failed") next.endedAt = now;
    return next;
  });
  return { ...thinking, plan };
}

export function finishThinking(thinking: Thinking, now: number): Thinking {
  if (thinking.endedAt) return thinking;
  const steps = thinking.steps.map((step, index) =>
    index === thinking.steps.length - 1 && !step.endedAt ? { ...step, endedAt: now } : step,
  );
  return { ...thinking, steps, endedAt: now };
}

export function formatSeconds(ms: number): string {
  return `${(Math.max(0, ms) / 1000).toFixed(1)} 秒`;
}
