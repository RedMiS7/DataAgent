import { Empty, Table } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { TableBlock } from "../types";

interface Props {
  table: TableBlock;
}

export default function ResultTable({ table }: Props) {
  if (!table.rows.length) {
    return <Empty description="没有查询到符合条件的数据" />;
  }

  const columns: ColumnsType<Record<string, unknown>> = table.columns.map((name, index) => {
    const numeric = table.rows.some((row) => typeof row[index] === "number");
    return {
      title: name,
      dataIndex: `c${index}`,
      key: `c${index}`,
      align: numeric ? "right" : "left",
      sorter: numeric
        ? (a, b) => Number(a[`c${index}`] ?? 0) - Number(b[`c${index}`] ?? 0)
        : undefined,
      render: (value: unknown) => (value === null || value === undefined ? "-" : String(value)),
    };
  });

  const dataSource = table.rows.map((row, rowIndex) => {
    const record: Record<string, unknown> = { key: rowIndex };
    row.forEach((value, columnIndex) => {
      record[`c${columnIndex}`] = value;
    });
    return record;
  });

  return (
    <div>
      <Table
        size="small"
        rowKey="key"
        columns={columns}
        dataSource={dataSource}
        pagination={{ pageSize: 10, showSizeChanger: true }}
        scroll={{ x: true }}
      />
      {table.truncated && (
        <div className="tag-note">结果超过上限，已截断为前 {table.row_count} 行。</div>
      )}
    </div>
  );
}