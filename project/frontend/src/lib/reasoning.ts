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
