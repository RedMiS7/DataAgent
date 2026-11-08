import type { AnswerPayload } from "../types";

export interface FailureNotice {
  /** "error": nothing was found; "warning": the answer is partial. */
  tone: "error" | "warning";
  message: string;
  suggestion: string;
}

const FALLBACK_ERROR = "这次没有得到结果。";
const FALLBACK_PARTIAL = "只完成了一部分，下面是已经查到的数据。";

/** The notice shown above a failed or partial answer; null for a normal one. */
export function failureNotice(answer: AnswerPayload): FailureNotice | null {
  const failure = answer.failure;
  if (answer.status === "error" || answer.status === "blocked") {
    return {
      tone: "error",
      message: failure?.message || FALLBACK_ERROR,
      suggestion: failure?.suggestion ?? "",
    };
  }
  if (answer.status === "partial") {
    return {
      tone: "warning",
      message: failure?.message || FALLBACK_PARTIAL,
      suggestion: failure?.suggestion ?? "",
    };
  }
  return null;
}

/** A failed answer has nothing to judge right or wrong; a refusal is not an answer. */
export function canGiveFeedback(answer: AnswerPayload | undefined): boolean {
  if (!answer?.log_id) return false;
  return !["refused", "error", "blocked"].includes(answer.status);
}

/** The SQL of a failed answer is the attempt that did not run. */
export function sqlTabLabel(answer: AnswerPayload): string {
  return answer.status === "error" ? "失败的 SQL" : "SQL";
}
