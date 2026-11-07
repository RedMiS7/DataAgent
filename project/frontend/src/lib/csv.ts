import type { TableBlock } from "../types";

function escapeCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  const text = String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function toCsv(table: TableBlock): string {
  const lines = [table.columns, ...table.rows].map((row) => row.map(escapeCell).join(","));
  return lines.join("\r\n");
}

export function downloadCsv(table: TableBlock, filename: string): void {
  // The BOM keeps Chinese text readable when the file is opened in Excel.
  const blob = new Blob(["﻿", toCsv(table)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
