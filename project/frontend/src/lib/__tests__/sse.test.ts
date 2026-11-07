import { describe, expect, it } from "vitest";
import { parseSseBlock, splitSseBuffer } from "../sse";

describe("sse parsing", () => {
  it("reads event and data lines", () => {
    expect(parseSseBlock('event: status\r\ndata: {"step":"a"}')).toEqual({
      event: "status",
      data: '{"step":"a"}',
    });
  });

  it("ignores blocks without data, such as pings", () => {
    expect(parseSseBlock(": ping")).toBeNull();
  });

  it("keeps an unfinished block in the buffer", () => {
    const { blocks, rest } = splitSseBuffer("event: a\ndata: 1\n\nevent: b\nda");
    expect(blocks).toEqual(["event: a\ndata: 1"]);
    expect(rest).toBe("event: b\nda");
  });
});
