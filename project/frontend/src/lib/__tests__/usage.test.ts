import { describe, expect, it } from "vitest";
import { usageDetail, usageLabel } from "../usage";

const USAGE = { model_calls: 7, tool_calls: 9, input_tokens: 8123, output_tokens: 1024, model_ms: 21460 };

describe("usage", () => {
  it("sums tokens in the short label", () => {
    expect(usageLabel(USAGE)).toBe("模型 7 次 · 9,147 tokens");
  });

  it("shows nothing without model calls or on old answers", () => {
    expect(usageLabel({ ...USAGE, model_calls: 0 })).toBe("");
    expect(usageLabel(undefined)).toBe("");
    expect(usageLabel(null)).toBe("");
  });

  it("splits input, output, model time and tools in the detail", () => {
    expect(usageDetail(USAGE)).toBe("输入 8,123 · 输出 1,024 tokens，模型累计 21.5 秒，工具 9 次");
  });
});
