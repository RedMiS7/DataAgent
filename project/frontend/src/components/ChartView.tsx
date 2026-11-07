import { useContext } from "react";
import { Empty } from "antd";
import ReactECharts from "echarts-for-react";
import { ThemeContext } from "../hooks/useTheme";
import { buildChartOption, chartHeight } from "../lib/chart";
import type { ChartSpec, TableBlock } from "../types";

interface Props {
  chart: ChartSpec;
  table: TableBlock;
}

export default function ChartView({ chart, table }: Props) {
  const theme = useContext(ThemeContext);
  const option = table.rows.length ? buildChartOption(table, chart, theme) : null;
  if (!option) {
    return <Empty description="结果形状不适合画图，请查看表格" />;
  }
  return (
    <ReactECharts
      option={option}
      notMerge
      style={{ height: chartHeight(table, chart), width: "100%" }}
    />
  );
}
