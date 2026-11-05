import ReactECharts from "echarts-for-react";
import { Empty } from "antd";
import type { ChartSpec, TableBlock } from "../types";

interface Props {
  chart: ChartSpec;
  table: TableBlock;
}

export default function ChartView({ chart, table }: Props) {
  if (chart.type === "table" || table.rows.length === 0) {
    return <Empty description="结果形状不适合画图，请查看表格" />;
  }

  const labels = table.rows.map((row) => String(row[0]));
  const values = table.rows.map((row) => Number(row[1] ?? 0));

  const option =
    chart.type === "pie"
      ? {
          tooltip: { trigger: "item" },
          legend: { bottom: 0, type: "scroll" },
          series: [
            {
              type: "pie",
              radius: ["35%", "65%"],
              data: labels.map((label, index) => ({ name: label, value: values[index] })),
            },
          ],
        }
      : {
          tooltip: { trigger: "axis" },
          grid: { left: 48, right: 24, top: 32, bottom: 48 },
          xAxis: {
            type: "category",
            data: labels,
            axisLabel: { rotate: labels.length > 8 ? 30 : 0 },
          },
          yAxis: { type: "value" },
          series: [{ type: chart.type, data: values, smooth: chart.type === "line" }],
        };

  return <ReactECharts className="chart-box" option={option} notMerge />;
}