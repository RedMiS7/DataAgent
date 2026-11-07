import { beforeEach, describe, expect, it, vi } from "vitest";
import { loadSavedMode, saveMode, suggestsAnalysis } from "../modes";

function fakeStorage() {
  const data = new Map<string, string>();
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, value),
  };
}

describe("saved mode", () => {
  beforeEach(() => vi.stubGlobal("localStorage", fakeStorage()));

  it("defaults to query", () => {
    expect(loadSavedMode()).toBe("query");
  });

  it("remembers the choice", () => {
    saveMode("analysis");
    expect(loadSavedMode()).toBe("analysis");
  });

  it("falls back to query when storage is blocked", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("blocked");
      },
    });
    expect(loadSavedMode()).toBe("query");
  });
});

describe("suggestsAnalysis", () => {
  it.each([
    ["分析一下最近30天的销量", true],
    ["最近生意怎么样？", true],
    ["退货率为什么涨了", true],
    ["上个月 GMV 是多少", false],
    ["销量最高的 10 个商品", false],
  ])("%s -> %s", (question, expected) => {
    expect(suggestsAnalysis(question)).toBe(expected);
  });
});
