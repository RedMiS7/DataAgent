import { describe, expect, it } from "vitest";
import { toCsv } from "../csv";

describe("toCsv", () => {
  it("escapes commas, quotes and empty values", () => {
    const csv = toCsv({
      columns: ["名称", "金额"],
      rows: [['A,"B"', 12.5], ["C", null]],
      truncated: false,
      row_count: 2,
    });
    expect(csv).toBe('名称,金额\r\n"A,""B""",12.5\r\nC,');
  });
});
