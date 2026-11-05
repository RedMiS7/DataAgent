import { useCallback, useEffect, useState } from "react";
import { App as AntApp, Button, Popconfirm, Space, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { fetchCases, reviewCase } from "../api";
import type { CaseOut } from "../types";

const STATUS_LABEL: Record<string, string> = {
  pending: "待审核",
  approved: "已通过",
  rejected: "已驳回",
};

const STATUS_COLOR: Record<string, string> = {
  pending: "orange",
  approved: "green",
  rejected: "red",
};

export default function ReviewPage() {
  const { message } = AntApp.useApp();
  const [rows, setRows] = useState<CaseOut[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRows(await fetchCases());
    } catch (error) {
      message.error(error instanceof Error ? error.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void load();
  }, [load]);

  const review = async (id: number, status: "approved" | "rejected") => {
    try {
      await reviewCase(id, status);
      message.success(status === "approved" ? "已通过" : "已驳回");
      await load();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "操作失败");
    }
  };

  const columns: ColumnsType<CaseOut> = [
    { title: "编号", dataIndex: "id", width: 70 },
    { title: "问题", dataIndex: "question" },
    {
      title: "SQL",
      dataIndex: "sql_text",
      render: (value: string) => <div className="mono">{value}</div>,
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 100,
      render: (value: string) => (
        <Tag color={STATUS_COLOR[value] ?? "default"}>{STATUS_LABEL[value] ?? value}</Tag>
      ),
    },
    { title: "创建时间", dataIndex: "created_at", width: 180 },
    {
      title: "操作",
      width: 160,
      render: (_, record) => (
        <Space>
          <Popconfirm title="确认通过？通过后进入检索案例库" onConfirm={() => void review(record.id, "approved")}>
            <Button size="small" type="primary" disabled={record.status === "approved"}>
              通过
            </Button>
          </Popconfirm>
          <Button size="small" danger disabled={record.status === "rejected"} onClick={() => void review(record.id, "rejected")}>
            驳回
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div className="review-layout">
      <Space style={{ marginBottom: 12 }}>
        <Button onClick={() => void load()} loading={loading}>
          刷新
        </Button>
      </Space>
      <Table rowKey="id" columns={columns} dataSource={rows} pagination={{ pageSize: 10 }} />
    </div>
  );
}