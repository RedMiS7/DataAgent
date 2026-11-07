import { describe, expect, it } from "vitest";
import { DEFAULT_MODELS, resolveModelId } from "../models";

describe("resolveModelId", () => {
  it("keeps a saved model that still exists", () => {
    expect(resolveModelId(DEFAULT_MODELS, "deepseek-reasoner")).toBe("deepseek-reasoner");
  });

  it("falls back to the first model", () => {
    expect(resolveModelId(DEFAULT_MODELS, "gone")).toBe(DEFAULT_MODELS[0].id);
    expect(resolveModelId(DEFAULT_MODELS, null)).toBe(DEFAULT_MODELS[0].id);
  });
});
