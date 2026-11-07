import { DEFAULT_MODELS, type ModelOption } from "./lib/models";
import type { AskMode } from "./lib/modes";
import { parseSseBlock, splitSseBuffer } from "./lib/sse";
import type {
  AnswerPayload,
  AttributionResult,
  CaseOut,
  MetaInfo,
  SetupStatus,
  StreamEvent,
} from "./types";

async function parseError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    return typeof body.detail === "string" ? body.detail : JSON.stringify(body);
  } catch {
    return `请求失败（${response.status}）`;
  }
}

export interface AskOptions {
  /** Sent ahead of backend support; the current API ignores it. */
  model?: string;
  mode?: AskMode;
  /** Re-asking: the backend drops the messages after this one first. */
  afterMessageId?: number;
  signal?: AbortSignal;
}

export async function askStream(
  question: string,
  sessionId: string,
  onEvent: (event: StreamEvent, data: Record<string, unknown>) => void,
  options: AskOptions = {},
): Promise<void> {
  const response = await fetch("/api/ask/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question,
      session_id: sessionId,
      model: options.model,
      mode: options.mode,
      after_message_id: options.afterMessageId,
    }),
    signal: options.signal,
  });
  if (!response.ok) {
    throw new Error(await parseError(response));
  }
  if (!response.body) {
    throw new Error("浏览器不支持流式响应");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const dispatch = (block: string) => {
    const message = parseSseBlock(block);
    if (!message) return;
    try {
      onEvent(message.event as StreamEvent, JSON.parse(message.data) as Record<string, unknown>);
    } catch {
      onEvent("error", { message: "无法解析服务端返回的数据" });
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const { blocks, rest } = splitSseBuffer(buffer);
    buffer = rest;
    blocks.forEach(dispatch);
  }
  if (buffer.trim()) dispatch(buffer);
}

export interface ModelList {
  models: ModelOption[];
  /** False while the list is the built-in fallback, so switching has no backend effect. */
  live: boolean;
}

/** Falls back to the built-in list until the backend offers `/api/models`. */
export async function fetchModels(): Promise<ModelList> {
  const fallback = { models: DEFAULT_MODELS, live: false };
  try {
    const response = await fetch("/api/models");
    if (!response.ok) return fallback;
    const models = (await response.json()) as ModelOption[];
    return Array.isArray(models) && models.length ? { models, live: true } : fallback;
  } catch {
    return fallback;
  }
}

export async function fetchSetupStatus(): Promise<SetupStatus> {
  const response = await fetch("/api/setup/status");
  if (!response.ok) throw new Error(await parseError(response));
  return (await response.json()) as SetupStatus;
}

export async function saveKeys(llmKey: string, embeddingKey: string): Promise<SetupStatus> {
  const response = await fetch("/api/setup/keys", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ llm_api_key: llmKey, embedding_api_key: embeddingKey }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return (await response.json()) as SetupStatus;
}

export async function retrySetup(): Promise<SetupStatus> {
  const response = await fetch("/api/setup/retry", { method: "POST" });
  if (!response.ok) throw new Error(await parseError(response));
  return (await response.json()) as SetupStatus;
}

export async function fetchMeta(): Promise<MetaInfo> {
  const response = await fetch("/api/meta");
  if (!response.ok) throw new Error(await parseError(response));
  return (await response.json()) as MetaInfo;
}

export async function askOnce(question: string, sessionId: string): Promise<AnswerPayload> {
  const response = await fetch("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, session_id: sessionId }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return (await response.json()) as AnswerPayload;
}

export async function attributeReturnRate(question: string): Promise<AttributionResult> {
  const response = await fetch("/api/attribution/return-rate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, session_id: "attribution" }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return (await response.json()) as AttributionResult;
}

export async function sendFeedback(logId: number, verdict: "correct" | "wrong"): Promise<void> {
  const response = await fetch("/api/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ log_id: logId, verdict }),
  });
  if (!response.ok) throw new Error(await parseError(response));
}

export async function fetchCases(status?: string): Promise<CaseOut[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  const response = await fetch(`/api/cases${query}`);
  if (!response.ok) throw new Error(await parseError(response));
  return (await response.json()) as CaseOut[];
}

export async function reviewCase(id: number, status: "approved" | "rejected"): Promise<void> {
  const response = await fetch(`/api/cases/${id}/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!response.ok) throw new Error(await parseError(response));
}