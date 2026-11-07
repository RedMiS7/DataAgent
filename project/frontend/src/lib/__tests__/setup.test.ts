import { describe, expect, it } from "vitest";
import type { SetupStatus, SetupStep } from "../../types";
import { needsKeys, setupHeading, stepResult } from "../setup";

const status = (patch: Partial<SetupStatus> = {}): SetupStatus => ({
  ready: false,
  llm_configured: true,
  embedding_configured: true,
  steps: [],
  orders_done: 0,
  orders_total: 0,
  error: "",
  ...patch,
});
const step = (patch: Partial<SetupStep>): SetupStep => ({
  key: "database",
  label: "连接数据库",
  state: "pending",
  detail: "",
  ...patch,
});

describe("stepResult", () => {
  it("shows order progress while generating data", () => {
    const running = step({ key: "demo_data", state: "running" });
    expect(stepResult(running, status({ orders_done: 5000, orders_total: 200000 })))
      .toBe("5,000 / 200,000 单");
  });

  it("prefers the backend detail", () => {
    expect(stepResult(step({ state: "done", detail: "已有数据，保持不变" }), status()))
      .toBe("已有数据，保持不变");
  });

  it("names plain states", () => {
    expect(stepResult(step({ state: "done" }), status())).toBe("完成");
    expect(stepResult(step({ state: "failed" }), status())).toBe("失败");
  });
});

describe("setup copy", () => {
  it("asks for keys until both are configured", () => {
    expect(needsKeys(status({ embedding_configured: false }))).toBe(true);
    expect(needsKeys(status())).toBe(false);
    expect(setupHeading(status({ llm_configured: false }))).toBe("开始前，填两个 Key");
    expect(setupHeading(status())).toBe("正在准备你的账本");
  });

  it("reports an interrupted setup first", () => {
    expect(setupHeading(status({ llm_configured: false, error: "连不上 MySQL" })))
      .toBe("准备过程中断了");
  });
});
