import { describe, expect, it } from "vitest";
import { addReasoning, addStep, finishThinking, formatSeconds, startThinking } from "../reasoning";

describe("thinking steps", () => {
  it("closes the previous step when a new one starts", () => {
    let thinking = startThinking(0);
    thinking = addStep(thinking, "retrieving", "检索中", 100);
    thinking = addStep(thinking, "executing", "执行中", 400);
    expect(thinking.steps).toHaveLength(2);
    expect(thinking.steps[0].endedAt).toBe(400);
    expect(thinking.steps[1].endedAt).toBeUndefined();
  });

  it("merges repeated events of the same step", () => {
    let thinking = addStep(startThinking(0), "retrying", "第一次", 10);
    thinking = addStep(thinking, "retrying", "第二次", 20);
    expect(thinking.steps).toHaveLength(1);
    expect(thinking.steps[0]).toMatchObject({ message: "第二次", startedAt: 10 });
  });

  it("finishes once and closes the open step", () => {
    const running = addStep(startThinking(0), "executing", "执行中", 10);
    const done = finishThinking(running, 50);
    expect(done.endedAt).toBe(50);
    expect(done.steps[0].endedAt).toBe(50);
    expect(finishThinking(done, 99).endedAt).toBe(50);
  });

  it("accumulates streamed reasoning text", () => {
    expect(addReasoning(addReasoning(startThinking(0), "先看"), "退款").text).toBe("先看退款");
  });

  it("formats durations", () => {
    expect(formatSeconds(4210)).toBe("4.2 秒");
    expect(formatSeconds(-5)).toBe("0.0 秒");
  });
});
