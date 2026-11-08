import { describe, expect, it } from "vitest";
import { canGiveFeedback, failureNotice, sqlTabLabel } from "../failure";
import type { AnswerPayload } from "../../types";

function answer(overrides: Partial<AnswerPayload> = {}): AnswerPayload {
  return {
    question: "上个月GMV是多少？",
    rewritten_question: "上个月GMV是多少？",
    sql: "SELECT 1",
    status: "ok",
    table: null,
    chart: null,
    summary: "上个月 GMV 为 100 元。",
    summary_source: "model",
    metrics: [],
    time_range: {},
    tables_used: [],
    retrieval: { tables: [], schema_items: [], metrics: [], cases: [], entities: [],
                 degraded: false, degraded_reason: "" },
    retries: 1,
    log_id: 7,
    degraded: false,
    ...overrides,
  };
}

const TIMEOUT = { kind: "timeout" as const, message: "超过 120 秒还没有完成，下面是已经查到的数据。",
                  suggestion: "可以稍后重试。" };

describe("failure notice", () => {
  it("shows nothing for a normal answer", () => {
    expect(failureNotice(answer())).toBeNull();
    expect(failureNotice(answer({ status: "empty" }))).toBeNull();
  });

  it("warns on a partial answer with the reason and what to try", () => {
    expect(failureNotice(answer({ status: "partial", failure: TIMEOUT }))).toEqual({
      tone: "warning", message: TIMEOUT.message, suggestion: "可以稍后重试。",
    });
  });

  it("shows a failed answer as an error", () => {
    const failure = { kind: "sql_failed" as const, message: "SQL 试了 3 次都没有成功。",
                      suggestion: "可以换个说法。" };
    expect(failureNotice(answer({ status: "error", summary: "", failure }))).toEqual({
      tone: "error", message: failure.message, suggestion: failure.suggestion,
    });
  });

  it("still says something when the reason is missing", () => {
    expect(failureNotice(answer({ status: "error", failure: null }))?.message).toBe("这次没有得到结果。");
    expect(failureNotice(answer({ status: "partial" }))?.message).toContain("已经查到的数据");
  });
});

describe("feedback and SQL label", () => {
  it("asks for feedback only on answers that found something", () => {
    expect(canGiveFeedback(answer())).toBe(true);
    expect(canGiveFeedback(answer({ status: "partial" }))).toBe(true);
    expect(canGiveFeedback(answer({ status: "error" }))).toBe(false);
    expect(canGiveFeedback(answer({ status: "refused" }))).toBe(false);
    expect(canGiveFeedback(answer({ log_id: null }))).toBe(false);
    expect(canGiveFeedback(undefined)).toBe(false);
  });

  it("names the SQL of a failed answer as the failed attempt", () => {
    expect(sqlTabLabel(answer({ status: "error" }))).toBe("失败的 SQL");
    expect(sqlTabLabel(answer())).toBe("SQL");
  });
});
