import { describe, expect, it } from "vitest";
import { conclusionOf, evidenceLabel, evidenceSql, pointQueries } from "../conclusion";
import type { AnswerPayload, Evidence } from "../../types";

const EVIDENCE: Evidence = { query: 2, rows: [0, 1], columns: ["期间", "退货率"], table: null };

function answer(overrides: Partial<AnswerPayload> = {}): AnswerPayload {
  return { status: "ok", summary: "s", sql: "SELECT 1", results: [], ...overrides } as AnswerPayload;
}

describe("conclusion", () => {
  it("shows a conclusion that has something to show", () => {
    const conclusion = { judgement: "退货率上升", points: [], advice: "", checked: true };
    expect(conclusionOf(answer({ conclusion }))).toEqual(conclusion);
    expect(conclusionOf(answer())).toBeNull();
    expect(conclusionOf(answer({ conclusion: { ...conclusion, judgement: "" } }))).toBeNull();
  });

  it("lists the queries a point cites once, in order", () => {
    expect(pointQueries({ claim: "c", evidence: [EVIDENCE, { ...EVIDENCE, query: 1 }, EVIDENCE] }))
      .toEqual([1, 2]);
  });

  it("labels the evidence with rows counted from 1", () => {
    expect(evidenceLabel(EVIDENCE)).toBe("查询2 · 第 1、2 行 · 期间、退货率");
    expect(evidenceLabel({ ...EVIDENCE, rows: [], columns: [] })).toBe("查询2");
  });

  it("finds the SQL of the cited query", () => {
    const results = [{ index: 2, purpose: "p", sql: "SELECT 2", table: null, chart: null }];
    expect(evidenceSql(answer({ results }), EVIDENCE)).toBe("SELECT 2");
    // A query answer shows one table, its own SQL.
    expect(evidenceSql(answer(), EVIDENCE)).toBe("SELECT 1");
    // A query that is not shown has no SQL to give.
    expect(evidenceSql(answer({ results }), { ...EVIDENCE, query: 3 })).toBe("");
  });
});
