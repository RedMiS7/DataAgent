import type { AnswerPayload, Conclusion, ConclusionPoint, Evidence } from "../types";

/** The conclusion to show as points with evidence; null shows the summary text instead. */
export function conclusionOf(answer: AnswerPayload): Conclusion | null {
  const conclusion = answer.conclusion;
  if (!conclusion || (!conclusion.judgement && conclusion.points.length === 0)) return null;
  return conclusion;
}

/** The queries a point cites, in order. */
export function pointQueries(point: ConclusionPoint): number[] {
  return [...new Set(point.evidence.map((item) => item.query))].sort((a, b) => a - b);
}

/** "查询2 · 第 1、2 行 · 退货率": rows are counted from 1 for the reader. */
export function evidenceLabel(evidence: Evidence): string {
  const parts = [`查询${evidence.query}`];
  if (evidence.rows.length) parts.push(`第 ${evidence.rows.map((row) => row + 1).join("、")} 行`);
  if (evidence.columns.length) parts.push(evidence.columns.join("、"));
  return parts.join(" · ");
}

/** The SQL of the cited query: one of the shown queries, or the answer's own. */
export function evidenceSql(answer: AnswerPayload, evidence: Evidence): string {
  const result = answer.results?.find((item) => item.index === evidence.query);
  return result?.sql ?? (answer.results?.length ? "" : answer.sql);
}
