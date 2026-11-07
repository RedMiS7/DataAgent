import type { CallUsage } from "../types";

const count = (value: number) => value.toLocaleString("en-US");

/** The short line under an answer; empty when no model was called. */
export function usageLabel(usage: CallUsage | null | undefined): string {
  if (!usage?.model_calls) return "";
  return `模型 ${usage.model_calls} 次 · ${count(usage.input_tokens + usage.output_tokens)} tokens`;
}

export function usageDetail(usage: CallUsage): string {
  return `输入 ${count(usage.input_tokens)} · 输出 ${count(usage.output_tokens)} tokens，`
    + `模型累计 ${(usage.model_ms / 1000).toFixed(1)} 秒，工具 ${usage.tool_calls} 次`;
}
