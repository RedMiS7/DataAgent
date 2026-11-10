import type { AnswerPayload, Clarify } from "../types";

/** Room for the backend's longer JSON (it adds spaces) under its 4000-character limit. */
const HISTORY_MAX_INTENT_CHARS = 3000;

/** "我理解为：…", with what was assumed; empty when there is nothing to show. */
export function understoodLine(answer: AnswerPayload): string {
  const text = answer.intent?.text ?? "";
  if (!text) return "";
  const assumed = answer.intent?.assumptions ?? [];
  return `我理解为：${text}` + (assumed.length ? `（${assumed.join("；")}）` : "");
}

/** The question asked back, when the answer is one. */
export function clarifyOf(answer: AnswerPayload | undefined): Clarify | null {
  if (answer?.status !== "clarify") return null;
  return answer.clarify ?? { question: answer.summary, options: [], round: 1 };
}

/** The intent sent back with the conversation; too long a structure is left out. */
export function historyIntent(answer: AnswerPayload): Record<string, unknown> | null {
  const data = answer.intent?.data;
  if (!data || Object.keys(data).length === 0) return null;
  return JSON.stringify(data).length <= HISTORY_MAX_INTENT_CHARS ? data : null;
}
