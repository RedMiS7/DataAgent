export type SummaryPart = string | { query: number };

export interface AnalysisSummary {
  headline: SummaryPart[];
  points: SummaryPart[][];
}

const CITATION = /\[查询(\d+)\]/g;
const POINT_MARKER = /^\s*(?:[-•·*]|\d+[.、)])\s*/;

export function splitCitations(text: string): SummaryPart[] {
  const parts: SummaryPart[] = [];
  let cursor = 0;
  for (const match of text.matchAll(CITATION)) {
    const before = text.slice(cursor, match.index);
    if (before) parts.push(before);
    parts.push({ query: Number(match[1]) });
    cursor = match.index + match[0].length;
  }
  const rest = text.slice(cursor);
  if (rest) parts.push(rest);
  return parts;
}

/** The first line is the judgment; every later line is one point. */
export function parseAnalysisSummary(text: string): AnalysisSummary {
  const lines = text.split("\n").map((line) => line.trim()).filter(Boolean);
  const [first = "", ...rest] = lines;
  return {
    headline: splitCitations(first.replace(POINT_MARKER, "")),
    points: rest.map((line) => splitCitations(line.replace(POINT_MARKER, ""))),
  };
}
