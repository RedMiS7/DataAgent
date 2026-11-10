import { describe, expect, it } from "vitest";
import type { ChartSpec, TableBlock } from "../../types";
import { buildChartOption, compactNumber, effectiveChartType, formatNumber, resolveAxes } from "../chart";

const table = (columns: string[], rows: unknown[][]): TableBlock => ({
  columns,
  rows,
  truncated: false,
  row_count: rows.length,
});
const spec = (x: string, y: string, type: ChartSpec["type"] = "bar"): ChartSpec => ({
  type,
  x_field: x,
  y_fields: [y],
  title: "",
});

describe("resolveAxes", () => {
  it("uses the proposed columns when they fit", () => {
    const data = table(["渠道", "订单数"], [["A", 1], ["B", 2]]);
    expect(resolveAxes(data, spec("渠道", "订单数"))).toEqual({ labelIndex: 0, valueIndex: 1 });
  });

  it("skips an id column and a text value column", () => {
    const data = table(["product_id", "name", "amount"], [[1, "甲", 9], [2, "乙", 8]]);
    expect(resolveAxes(data, spec("product_id", "name"))).toEqual({ labelIndex: 1, valueIndex: 2 });
  });

  it("keeps a proposed identifier when the labels are text", () => {
    const data = table(["label", "reference_id", "amount"], [["甲", "101", 9], ["乙", "102", 8]]);
    expect(resolveAxes(data, spec("reference_id", "amount"))).toEqual({ labelIndex: 1, valueIndex: 2 });
  });

  it("returns null when nothing is measurable", () => {
    const data = table(["a", "b"], [["x", "y"], ["z", "w"]]);
    expect(resolveAxes(data, spec("a", "b"))).toBeNull();
  });
});

describe("buildChartOption", () => {
  it("switches to horizontal bars for long labels", () => {
    const data = table(["商品", "销量"], [["手机配件热销款185号", 3], ["美妆热销款77号", 2]]);
    const option = buildChartOption(data, spec("商品", "销量"), "light") as {
      yAxis: { type: string };
    };
    expect(option.yAxis.type).toBe("category");
  });

  it("has no option for table results", () => {
    expect(buildChartOption(table(["a", "b"], [["x", 1]]), spec("a", "b", "table"), "dark")).toBeNull();
  });

  it("keeps a missing measure as a gap", () => {
    const data = table(["类别", "数量"], [["甲", 8], ["乙", null]]);
    const option = buildChartOption(data, spec("类别", "数量"), "light") as {
      series: { data: (number | null)[] }[];
    };
    expect(option.series[0].data).toEqual([8, null]);
  });
});

describe("effectiveChartType", () => {
  it("treats date labels as a line even when the backend proposes bars", () => {
    const data = table(["dt", "gmv"], [["2026-10-02", 3], ["2026-10-03", 2]]);
    expect(effectiveChartType(data, spec("dt", "gmv"))).toBe("line");
  });

  it("keeps the proposed type for categories", () => {
    const data = table(["渠道", "订单"], [["A", 1], ["B", 2]]);
    expect(effectiveChartType(data, spec("渠道", "订单", "pie"))).toBe("pie");
  });

  it("does not treat malformed date-like categories as a time series", () => {
    const data = table(["label", "value"], [["2026-02-30", 3], ["2026-13-01", 2]]);
    expect(effectiveChartType(data, spec("label", "value"))).toBe("bar");
  });
});

describe("number formatting", () => {
  it("groups digits and leaves text alone", () => {
    expect(formatNumber(9072501.5)).toBe("9,072,501.5");
    expect(formatNumber("中通")).toBe("中通");
  });

  it("compacts axis values", () => {
    expect(compactNumber(386272)).toBe("38.6万");
    expect(compactNumber(250000000)).toBe("2.5亿");
  });
});
