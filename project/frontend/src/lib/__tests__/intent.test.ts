import { describe, expect, it } from "vitest";
import { clarifyOf, historyIntent, understoodLine } from "../intent";
import type { AnswerPayload } from "../../types";

function answer(overrides: Partial<AnswerPayload> = {}): AnswerPayload {
  return { status: "ok", summary: "3.9%", ...overrides } as AnswerPayload;
}

const INTENT = {
  text: "时间：最近30天；指标：退货率",
  assumptions: ["未指定时间，按最近 30 天统计"],
  data: { route: "query", metrics: [{ name: "退货率" }] },
};

describe("intent", () => {
  it("shows how the question was understood, with what was assumed", () => {
    expect(understoodLine(answer({ intent: INTENT }))).toBe(
      "我理解为：时间：最近30天；指标：退货率（未指定时间，按最近 30 天统计）");
    expect(understoodLine(answer({ intent: { ...INTENT, assumptions: [] } }))).toBe(
      "我理解为：时间：最近30天；指标：退货率");
  });

  it("shows nothing without an understanding", () => {
    expect(understoodLine(answer())).toBe("");
    expect(understoodLine(answer({ intent: { text: "", assumptions: [], data: {} } }))).toBe("");
  });

  it("reads the question asked back", () => {
    const clarify = { question: "你想看哪个？", options: ["退货率", "退款金额"], round: 1 };
    expect(clarifyOf(answer({ status: "clarify", clarify }))).toEqual(clarify);
    // Without the structure, the summary is the question.
    expect(clarifyOf(answer({ status: "clarify", summary: "你想看哪个？" }))).toEqual(
      { question: "你想看哪个？", options: [], round: 1 });
    expect(clarifyOf(answer({ clarify }))).toBeNull();
    expect(clarifyOf(undefined)).toBeNull();
  });

  it("sends the structure back, unless it is empty or too long", () => {
    expect(historyIntent(answer({ intent: INTENT }))).toEqual(INTENT.data);
    expect(historyIntent(answer())).toBeNull();
    expect(historyIntent(answer({ intent: { ...INTENT, data: {} } }))).toBeNull();
    expect(historyIntent(answer({ intent: { ...INTENT, data: { long: "长".repeat(3001) } } })))
      .toBeNull();
  });
});
