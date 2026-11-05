import type { AnswerPayload, AttributionResult, CaseOut, StreamEvent } from "./types";

async function parseError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    return typeof body.detail === "string" ? body.detail : JSON.stringify(body);
  } catch {
    return `请求失败（${response.status}）`;
  }
}

export async function askStream(
  question: string,
  sessionId: string,
  onEvent: (event: StreamEvent, data: Record<string, unknown>) => void,
): Promise<void> {
  const response = await fetch("/api/ask/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, session_id: sessionId }),
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

  const dispatch = (chunk: string) => {
    let event: StreamEvent = "status";
    const dataLines: string[] = [];
    for (const line of chunk.split("\n")) {
      if (line.startsWith("event:")) {
        event = line.slice(6).trim() as StreamEvent;
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }
    if (!dataLines.length) return;
    const raw = dataLines.join("\n");
    try {
      onEvent(event, JSON.parse(raw) as Record<string, unknown>);
    } catch {
      onEvent("error", { message: "无法解析服务端返回的数据" });
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split(/\r?\n\r?\n/);
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      if (chunk.trim()) dispatch(chunk);
    }
  }
  if (buffer.trim()) dispatch(buffer);
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