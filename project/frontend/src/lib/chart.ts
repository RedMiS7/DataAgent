import type { ChartSpec, TableBlock } from "../types";
import type { Theme } from "../hooks/useTheme";

export interface ChartTokens {
  series: string[];
  ink: string;
  muted: string;
  grid: string;
  surface: string;
}

/** Both palettes pass the dataviz validator against their own surface. */
export const CHART_TOKENS: Record<Theme, ChartTokens> = {
  light: {
    series: ["#0A7A5C", "#C2691E", "#3B6FB6", "#B3475A", "#7A5BB0"],
    ink: "#1E2421",
    muted: "#6A716B",
    grid: "#E9E4D8",
    surface: "#FFFDF8",
  },
  dark: {
    series: ["#30A07C", "#CF7C2C", "#5A90E8", "#DE5F7A", "#9B74E8"],
    ink: "#ECEDE7",
    muted: "#9AA29B",
    grid: "#2E3531",
    surface: "#1C211E",
  },
};

const MAX_POINTS = 30;

export interface ChartAxes {
  labelIndex: number;
  valueIndex: number;
}

function isNumericColumn(table: TableBlock, index: number): boolean {
  const values = table.rows.map((row) => row[index]).filter((value) => value != null);
  return values.length > 0 && values.every((value) =>
    (typeof value === "number" || (typeof value === "string" && value.trim() !== "")) &&
    Number.isFinite(Number(value)),
  );
}

function isIdColumn(name: string): boolean {
  return /(^|_)id$/i.test(name);
}

/** Keep valid proposed axes and repair axes in older saved answers. */
export function resolveAxes(table: TableBlock, chart: ChartSpec): ChartAxes | null {
  const columns = table.columns;
  const proposed = Math.max(0, columns.indexOf(chart.x_field));
  const textIndex = columns.findIndex((_, index) => !isNumericColumn(table, index));
  const numericId = isIdColumn(columns[proposed]) &&
    table.rows.some((row) => typeof row[proposed] === "number");
  const labelIndex = numericId && textIndex >= 0 ? textIndex : proposed;

  const wanted = columns.indexOf(chart.y_fields[0] ?? "");
  const candidates = [wanted, ...columns.keys()].filter(
    (index) =>
      index >= 0 && index !== labelIndex && !isIdColumn(columns[index]) &&
      isNumericColumn(table, index),
  );
  return candidates.length ? { labelIndex, valueIndex: candidates[0] } : null;
}

const DATE_LABEL = /^(\d{4}-\d{2})(?:-(\d{2})(?:[T ].+)?)?$/;

function isDateLabel(value: unknown): boolean {
  if (typeof value !== "string") return false;
  const parts = DATE_LABEL.exec(value);
  if (!parts) return false;
  const day = `${parts[1]}-${parts[2] ?? "01"}`;
  const parsed = new Date(`${day}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().startsWith(day) &&
    (value.length <= 10 || Number.isFinite(Date.parse(value)));
}

/** Recognize date labels in saved answers with older chart specs. */
export function effectiveChartType(table: TableBlock, chart: ChartSpec): ChartSpec["type"] {
  const axes = resolveAxes(table, chart);
  if (!axes || chart.type === "table" || table.rows.length < 2) return chart.type;
  const labels = table.rows.map((row) => row[axes.labelIndex]);
  const dated = labels.every(isDateLabel) && new Set(labels).size === labels.length;
  return dated ? "line" : chart.type;
}

export function formatNumber(value: unknown): string {
  const number = Number(value);
  if (value === null || value === undefined || value === "" || !Number.isFinite(number)) {
    return String(value ?? "-");
  }
  return number.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

export function compactNumber(value: number): string {
  const size = Math.abs(value);
  if (size >= 100_000_000) return `${(value / 100_000_000).toFixed(1)}亿`;
  if (size >= 10_000) return `${(value / 10_000).toFixed(1)}万`;
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

export function buildChartOption(table: TableBlock, chart: ChartSpec, theme: Theme): object | null {
  const axes = resolveAxes(table, chart);
  const type = effectiveChartType(table, chart);
  if (!axes || type === "table") return null;

  const tokens = CHART_TOKENS[theme];
  const rows = table.rows.slice(0, MAX_POINTS);
  const labels = rows.map((row) => String(row[axes.labelIndex]));
  const values = rows.map((row) => row[axes.valueIndex] == null ? null : Number(row[axes.valueIndex]));
  const seriesName = table.columns[axes.valueIndex];
  const text = { color: tokens.muted, fontSize: 12 };
  const tooltip = {
    backgroundColor: tokens.surface,
    borderColor: tokens.grid,
    textStyle: { color: tokens.ink, fontSize: 13 },
    valueFormatter: (value: unknown) => formatNumber(value),
  };

  if (type === "pie") {
    return {
      color: tokens.series,
      tooltip: { ...tooltip, trigger: "item" },
      legend: { bottom: 0, type: "scroll", textStyle: text, icon: "circle", itemWidth: 8 },
      series: [{
        name: seriesName,
        type: "pie",
        radius: ["46%", "70%"],
        center: ["50%", "45%"],
        itemStyle: { borderColor: tokens.surface, borderWidth: 2 },
        label: { color: tokens.ink, fontSize: 12 },
        data: labels.map((name, index) => ({ name, value: values[index] })),
      }],
    };
  }

  const valueAxis = {
    type: "value",
    axisLabel: { ...text, formatter: compactNumber },
    splitLine: { lineStyle: { color: tokens.grid } },
  };
  const longLabels = type === "bar" && labels.some((label) => label.length > 6);
  const categoryAxis = {
    type: "category",
    data: labels,
    inverse: longLabels,
    axisTick: { show: false },
    axisLine: { lineStyle: { color: tokens.grid } },
    axisLabel: { ...text, rotate: !longLabels && labels.length > 8 ? 30 : 0 },
  };
  const series = type === "line"
    ? {
        name: seriesName,
        type: "line",
        data: values,
        smooth: false,
        symbol: "circle",
        symbolSize: 8,
        showSymbol: labels.length <= 12,
        lineStyle: { width: 2 },
        itemStyle: { borderColor: tokens.surface, borderWidth: 2 },
      }
    : {
        name: seriesName,
        type: "bar",
        data: values,
        barMaxWidth: 28,
        itemStyle: { borderRadius: longLabels ? [0, 4, 4, 0] : [4, 4, 0, 0] },
      };

  return {
    color: tokens.series,
    tooltip: { ...tooltip, trigger: "axis", axisPointer: { type: type === "line" ? "line" : "shadow" } },
    grid: { left: 8, right: 24, top: 16, bottom: 8, containLabel: true },
    xAxis: longLabels ? valueAxis : categoryAxis,
    yAxis: longLabels ? categoryAxis : valueAxis,
    series: [series],
  };
}

export function chartHeight(table: TableBlock, chart: ChartSpec): number {
  const rows = Math.min(table.rows.length, MAX_POINTS);
  return effectiveChartType(table, chart) === "bar" ? Math.max(280, rows * 30 + 40) : 320;
}
