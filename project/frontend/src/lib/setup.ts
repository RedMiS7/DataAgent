import type { SetupStatus, SetupStep } from "../types";

export function stepResult(step: SetupStep, status: SetupStatus): string {
  if (step.detail) return step.detail;
  if (step.state === "running" && step.key === "demo_data" && status.orders_total > 0) {
    const done = status.orders_done.toLocaleString("zh-CN");
    return `${done} / ${status.orders_total.toLocaleString("zh-CN")} 单`;
  }
  const labels: Record<SetupStep["state"], string> = {
    pending: "等待中",
    running: "进行中",
    done: "完成",
    waiting: "等待 Key",
    skipped: "已跳过",
    failed: "失败",
  };
  return labels[step.state];
}

export function needsKeys(status: SetupStatus): boolean {
  return !status.llm_configured || !status.embedding_configured;
}

export function setupHeading(status: SetupStatus): string {
  if (status.error) return "准备过程中断了";
  return needsKeys(status) ? "开始前，填两个 Key" : "正在准备你的账本";
}
