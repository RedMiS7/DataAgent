/** The mode an answer ran in. */
export type AnswerMode = "query" | "analysis";
/** The mode a question is sent with; "auto" lets the backend choose. */
export type AskMode = "auto" | AnswerMode;

export const DEFAULT_MODE: AskMode = "auto";

const MODES: AskMode[] = ["auto", "query", "analysis"];

export const MODE_OPTIONS: { id: AskMode; label: string; description: string }[] = [
  { id: "auto", label: "自动", description: "根据问题自动选择查询或分析" },
  { id: "query", label: "查询", description: "查具体数字和排行，回答快" },
  { id: "analysis", label: "分析", description: "多查几组数据，给出判断和建议，耗时更长" },
];

const STORAGE_KEY = "dataagent.mode";
const ANALYSIS_WORDS = /分析|怎么样|为什么|建议|结论|原因|有什么问题|怎么办/;

export function loadSavedMode(): AskMode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    return MODES.find((mode) => mode === saved) ?? DEFAULT_MODE;
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
