import { describe, expect, it } from "vitest";
import { parseAnalysisSummary, splitCitations } from "../summary";

describe("splitCitations", () => {
  it("separates text and citations", () => {
    expect(splitCitations("GMV 增长 6.3% [查询1][查询2]，不错")).toEqual([
      "GMV 增长 6.3% ",
      { query: 1 },
      { query: 2 },
      "，不错",
    ]);
  });

  it("keeps text without citations whole", () => {
    expect(splitCitations("整体平稳")).toEqual(["整体平稳"]);
  });
});

describe("parseAnalysisSummary", () => {
  it("takes the first line as the judgment and the rest as points", () => {
    const parsed = parseAnalysisSummary("整体增长。\n\n- 鞋靴增长 [查询2]\n2. 建议：加投放\n");
    expect(parsed.headline).toEqual(["整体增长。"]);
    expect(parsed.points).toEqual([["鞋靴增长 ", { query: 2 }], ["建议：加投放"]]);
  });

  it("handles a single line", () => {
    expect(parseAnalysisSummary("整体平稳").points).toEqual([]);
  });

  it("does not treat a leading number as a list marker unless it has one", () => {
    expect(parseAnalysisSummary("30 天内整体平稳").headline).toEqual(["30 天内整体平稳"]);
  });
});
