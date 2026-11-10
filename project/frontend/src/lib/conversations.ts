import type { AnswerPayload, HistoryMessage, HistoryQuery } from "../types";
import { historyIntent } from "./intent";
import type { AskMode } from "./modes";
import type { Thinking } from "./reasoning";

export type MessageState = "streaming" | "done" | "stopped" | "error";

export interface UserMessage {
  id: string;
  role: "user";
  text: string;
  mode?: AskMode;
}

export interface AssistantMessage {
  id: string;
  role: "assistant";
  state: MessageState;
  thinking: Thinking;
  answer?: AnswerPayload;
  error?: string;
  verdict?: "correct" | "wrong";
}

export type ChatMessage = UserMessage | AssistantMessage;

export interface Conversation {
  id: string;
  title: string;
  messages: ChatMessage[];
  updatedAt: number;
}

export interface ConversationGroup {
  label: string;
  items: Conversation[];
}

const STORAGE_KEY = "dataagent.conversations";
const ACTIVE_KEY = "dataagent.activeConversation";
const MAX_CONVERSATIONS = 30;
const TITLE_LENGTH = 24;
const DAY_MS = 24 * 60 * 60 * 1000;

export function newId(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
}

export function createConversation(now: number): Conversation {
  return { id: newId("session"), title: "新对话", messages: [], updatedAt: now };
}

export function deriveTitle(question: string): string {
  const text = question.trim().replace(/\s+/g, " ");
  return text.length > TITLE_LENGTH ? `${text.slice(0, TITLE_LENGTH)}…` : text;
}

function startOfDay(time: number): number {
  const date = new Date(time);
  date.setHours(0, 0, 0, 0);
  return date.getTime();
}

export function groupConversations(items: Conversation[], now: number): ConversationGroup[] {
  const today = startOfDay(now);
  const buckets: ConversationGroup[] = [
    { label: "今天", items: [] },
    { label: "昨天", items: [] },
    { label: "近 7 天", items: [] },
    { label: "更早", items: [] },
  ];
  const sorted = [...items].sort((a, b) => b.updatedAt - a.updatedAt);
  for (const item of sorted) {
    const age = today - startOfDay(item.updatedAt);
    const index = age <= 0 ? 0 : age <= DAY_MS ? 1 : age <= 7 * DAY_MS ? 2 : 3;
    buckets[index].items.push(item);
  }
  return buckets.filter((bucket) => bucket.items.length > 0);
}

export function searchConversations(items: Conversation[], keyword: string): Conversation[] {
  const needle = keyword.trim().toLowerCase();
  if (!needle) return items;
  return items.filter((item) => {
    if (item.title.toLowerCase().includes(needle)) return true;
    return item.messages.some(
      (message) => message.role === "user" && message.text.toLowerCase().includes(needle),
    );
  });
}

/** A reload ends any running stream, so restored messages must not look live. */
export function settleMessages(items: Conversation[], now: number): Conversation[] {
  return items.map((item) => ({
    ...item,
    messages: item.messages.map((message) =>
      message.role === "assistant" && message.state === "streaming"
        ? { ...message, state: "stopped", thinking: { ...message.thinking, endedAt: now } }
        : message,
    ),
  }));
}

export function loadConversations(now: number): Conversation[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? settleMessages(parsed as Conversation[], now) : [];
  } catch {
    return [];
  }
}

export function loadActiveId(): string | null {
  try {
    return localStorage.getItem(ACTIVE_KEY);
  } catch {
    return null;
  }
}

export function saveActiveId(id: string): void {
  try {
    localStorage.setItem(ACTIVE_KEY, id);
  } catch {
    // Storage may be blocked; a reload then opens a new conversation.
  }
}

/** Reopen the last conversation. One left empty was never stored, so start a new one. */
export function restoreConversations(
  stored: Conversation[],
  activeId: string | null,
  fresh: Conversation,
): { items: Conversation[]; activeId: string } {
  if (stored.some((item) => item.id === activeId)) {
    return { items: stored, activeId: activeId as string };
  }
  return { items: [fresh, ...stored], activeId: fresh.id };
}

/** The backend rejects longer history messages, purposes and more queries. */
const HISTORY_MAX_CHARS = 4000;
const HISTORY_MAX_PURPOSE = 200;
const HISTORY_MAX_QUERIES = 10;
const TIME_RANGE_KEYS = ["label", "start", "end_exclusive"];

/**
 * The queries an answer rests on, so a follow-up can count the same way.
 * A failed answer's SQL did not run, so it is left out.
 */
export function answerQueries(answer: AnswerPayload): HistoryQuery[] {
  if (!["ok", "empty", "partial"].includes(answer.status)) return [];
  const queries = answer.results?.length
    ? answer.results.map((result) => ({ purpose: result.purpose, sql: result.sql }))
    : answer.sql ? [{ purpose: answer.question, sql: answer.sql }] : [];
  return queries
    .filter((query) => query.sql)
    .slice(0, HISTORY_MAX_QUERIES)
    .map((query) => ({
      purpose: query.purpose.slice(0, HISTORY_MAX_PURPOSE),
      sql: query.sql.slice(0, HISTORY_MAX_CHARS),
    }));
}

function timeRange(answer: AnswerPayload): Record<string, string> {
  const range: Record<string, string> = {};
  for (const key of TIME_RANGE_KEYS) {
    const value = answer.time_range?.[key];
    if (value) range[key] = value.slice(0, 64);
  }
  return range;
}

/** A reply as the backend reads it; failed and stopped replies say how they ended. */
function replyHistory(message: AssistantMessage): HistoryMessage | null {
  const { answer } = message;
  if (message.state === "done" && answer) {
    const reply: HistoryMessage = {
      role: "assistant",
      content: (answer.summary || answer.failure?.message || "").slice(0, HISTORY_MAX_CHARS),
      mode: answer.mode ?? "query",
    };
    // Answers saved before statuses were sent have none; the backend treats them as normal.
    if (answer.status) reply.status = answer.status;
    const queries = answerQueries(answer);
    if (queries.length) {
      reply.queries = queries;
      reply.time_range = timeRange(answer);
    }
    const intent = historyIntent(answer);
    if (intent) reply.intent = intent;
    return reply;
  }
  if (message.state === "error") {
    return { role: "assistant", content: (message.error ?? "").slice(0, HISTORY_MAX_CHARS),
             status: "error" };
  }
  if (message.state === "stopped") {
    return { role: "assistant", content: "", status: "stopped" };
  }
  // Still streaming: not part of the conversation yet.
  return null;
}

/**
 * The conversation sent with each question. Every question keeps its reply, including
 * replies that failed or stopped; a question without one reads as still open.
 */
export function buildHistory(messages: ChatMessage[]): HistoryMessage[] {
  const history: HistoryMessage[] = [];
  for (const message of messages) {
    if (message.role === "user") {
      history.push({ role: "user", content: message.text });
      continue;
    }
    const reply = replyHistory(message);
    if (reply) history.push(reply);
  }
  return history;
}

export function saveConversations(items: Conversation[]): void {
  const stored = [...items]
    .filter((item) => item.messages.length > 0)
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, MAX_CONVERSATIONS);
  for (let keep = stored.length; keep >= 0; keep = keep > 1 ? Math.floor(keep / 2) : keep - 1) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(stored.slice(0, keep)));
      return;
    } catch {
      // Storage is full or blocked: retry with fewer conversations.
    }
  }
}
