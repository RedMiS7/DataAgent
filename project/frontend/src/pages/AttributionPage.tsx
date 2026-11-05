import { useMemo, useState } from "react";
import { Alert, App as AntApp, Button, Collapse, Input, Space, Statistic, Table, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { attributeReturnRate } from "../api";
import type { AttributionFactor, AttributionResult } from "../types";

const { Text, Title } = Typography;

const DIMENSIONS: { key: string; label: string }[] = [
  { key: "product", label: "按商品下钻" },
  { key: "channel", label: "按渠道下钻" },
  { key: "carrier", label: "按物流承运商下钻" },
];

function factorColumns(): ColumnsType<AttributionFactor> {
  return [
    { title: "名称", dataIndex: "name" },
    {
      title: "本期退款",
      dataIndex: "current_refund",
      align: "right",
      render: (value: number) => value.toFixed(2),
    },
    {
      title: "对比期退款",
      dataIndex: "previous_refund",
      align: "right",
      render: (value: number) => value.toFixed(2),
    },
    {
      title: "变化",
      dataIndex: "delta",
      align: "right",
      render: (value: number) => (
        <span style={{ color: value >= 0 ? "#cf1322" : "#3f8600" }}>
          {value >= 0 ? "+" : ""}
          {value.toFixed(2)}
        </span>
      ),
    },
    {
      title: "占整体变化",
      dataIndex: "share",
      align: "right",
      render: (value: number) => `${value.toFixed(1)}%`,
    },
  ];
}

export default function AttributionPage() {
  const { message } = AntApp.useApp();
  const [question, setQuestion] = useState("最近的退货率为什么涨了？");
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<AttributionResult | null>(null);

  const columns = useMemo(factorColumns, []);

  const run = async () => {
    setLoading(true);
    try {
      setData(await attributeReturnRate(question));
    } catch (error) {
      message.error(error instanceof Error ? error.message : "分析失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <Space.Compact style={{ width: "100%" }}>
        <Input
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="例如：最近的退货率为什么涨了？"
        />
        <Button type="primary" loading={loading} onClick={() => void run()}>
          分析
        </Button>
      </Space.Compact>

      {data && (
        <div style={{ marginTop: 20 }}>
          <Alert type={data.delta_points >= 0 ? "warning" : "success"} showIcon
                 message={data.summary} style={{ marginBottom: 8 }} />
          <div style={{ marginBottom: 16 }}>
            <span className="tag-note">
              结论来源：{data.summary_source === "agent" ? "AgentScope Agent" : "模板"}
            </span>
          </div>

          <Space size={48} wrap>
            <Statistic title="本期退货率" value={data.current_rate} suffix="%" precision={2} />
            <Statistic title="对比期退货率" value={data.previous_rate} suffix="%" precision={2} />
            <Statistic title="变化" value={data.delta_points} suffix="个百分点" precision={2} />
            <Statistic title="本期退款金额" value={data.total_current_refund} precision={2} />
          </Space>

          <div className="meta-grid" style={{ marginTop: 16 }}>
            <div>
              本期：<strong>{data.current.start} 至 {data.current.end_exclusive}</strong>（不含右端）
            </div>
            <div>
              对比期：<strong>{data.previous.start} 至 {data.previous.end_exclusive}</strong>
            </div>
            <div>
              口径：<Text type="secondary">退货率为退款金额除以同期 GMV，退款按完成时间归属</Text>
            </div>
          </div>

          {DIMENSIONS.map((dimension) => {
            const rows = data.factors.filter((factor) => factor.dimension === dimension.key).slice(0, 5);
            if (!rows.length) return null;
            return (
              <div key={dimension.key} style={{ marginTop: 20 }}>
                <Title level={5}>{dimension.label}</Title>
                <Table size="small" rowKey={(record) => `${record.dimension}-${record.name}`}
                       columns={columns} dataSource={rows} pagination={false} />
              </div>
            );
          })}

          <Collapse
            ghost
            style={{ marginTop: 16 }}
            items={[{ key: "sql", label: "查看归因 SQL",
                      children: data.sql.map((sql) => <div key={sql} className="mono">{sql}</div>) }]}
          />
        </div>
      )}
    </div>
  );
}