export type AskMode = "query" | "analysis";

export const DEFAULT_MODE: AskMode = "query";

export const MODE_OPTIONS: { id: AskMode; label: string; description: string }[] = [
  { id: "query", label: "查询", description: "查具体数字和排行，回答快" },
  { id: "analysis", label: "分析", description: "多查几组数据，给出判断和建议，耗时更长" },
];

const STORAGE_KEY = "dataagent.mode";
const ANALYSIS_WORDS = /分析|怎么样|为什么|建议|结论|原因|有什么问题|怎么办/;

export function loadSavedMode(): AskMode {
  try {
    return localStorage.getItem(STORAGE_KEY) === "analysis" ? "analysis" : DEFAULT_MODE;
  } catch {
    return DEFAULT_MODE;
  }
}

export function saveMode(mode: AskMode): void {
  try {
    localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // Storage may be blocked; the choice then lasts for this page only.
  }
}

/** A hint only: the user decides whether to switch. */
export function suggestsAnalysis(question: string): boolean {
  return ANALYSIS_WORDS.test(question);
}
