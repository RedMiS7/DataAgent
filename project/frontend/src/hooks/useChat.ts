import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { askStream, sendFeedback } from "../api";
import {
  buildHistory,
  createConversation,
  deriveTitle,
  loadActiveId,
  loadConversations,
  newId,
  restoreConversations,
  saveActiveId,
  saveConversations,
  type AssistantMessage,
  type ChatMessage,
  type Conversation,
} from "../lib/conversations";
import { DEFAULT_MODE, type AskMode } from "../lib/modes";
import {
  addReasoning,
  addStep,
  finishThinking,
  setPlan,
  startThinking,
  updatePlanStep,
} from "../lib/reasoning";
import type { AnswerPayload, PlanStep } from "../types";

function initialState(): { items: Conversation[]; activeId: string } {
  const now = Date.now();
  return restoreConversations(loadConversations(now), loadActiveId(), createConversation(now));
}

export interface ChatApi {
  conversations: Conversation[];
  active: Conversation;
  streaming: boolean;
  send: (text: string, model: string, mode: AskMode) => void;
  stop: () => void;
  regenerate: (assistantId: string, model: string, mode?: AskMode) => void;
  editAndResend: (userId: string, text: string, model: string) => void;
  giveFeedback: (assistantId: string, verdict: "correct" | "wrong") => Promise<void>;
  newConversation: () => void;
  selectConversation: (id: string) => void;
  renameConversation: (id: string, title: string) => void;
  deleteConversation: (id: string) => void;
}

export function useChat(): ChatApi {
  const [state] = useState(initialState);
  const [items, setItems] = useState<Conversation[]>(state.items);
  const [activeId, setActiveId] = useState(state.activeId);
  const [streamingId, setStreamingId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (streamingId === null) saveConversations(items);
  }, [items, streamingId]);

  useEffect(() => saveActiveId(activeId), [activeId]);

  const active = useMemo(
    () => items.find((item) => item.id === activeId) ?? items[0],
    [items, activeId],
  );

  const patchAssistant = useCallback(
    (conversationId: string, messageId: string,
     patch: (message: AssistantMessage) => AssistantMessage) => {
      setItems((prev) =>
        prev.map((item) =>
          item.id !== conversationId
            ? item
            : {
                ...item,
                messages: item.messages.map((message) =>
                  message.id === messageId && message.role === "assistant"
                    ? patch(message)
                    : message,
                ),
              },
        ),
      );
    },
    [],
  );

  const run = useCallback(
    (conversationId: string, base: ChatMessage[], text: string, model: string, mode: AskMode) => {
      const now = Date.now();
      const user: ChatMessage = { id: newId("u"), role: "user", text, mode };
      const assistant: AssistantMessage = {
        id: newId("a"),
        role: "assistant",
        state: "streaming",
        thinking: startThinking(now),
      };
      setItems((prev) =>
        prev.map((item) =>
          item.id !== conversationId
            ? item
            : {
                ...item,
                title: base.length === 0 ? deriveTitle(text) : item.title,
                messages: [...base, user, assistant],
                updatedAt: now,
              },
        ),
      );

      const controller = new AbortController();
      abortRef.current = controller;
      setStreamingId(conversationId);
      const patch = (fn: (message: AssistantMessage) => AssistantMessage) =>
        patchAssistant(conversationId, assistant.id, fn);

      askStream(
        text,
        conversationId,
        (event, data) => {
          const time = Date.now();
          if (event === "status") {
            patch((message) => ({
              ...message,
              thinking: addStep(message.thinking, String(data.step ?? ""),
                                String(data.message ?? ""), time),
            }));
          } else if (event === "reasoning") {
            patch((message) => ({
              ...message,
              thinking: addReasoning(message.thinking, String(data.delta ?? "")),
            }));
          } else if (event === "plan") {
            patch((message) => ({
              ...message,
              thinking: setPlan(message.thinking, (data.steps ?? []) as PlanStep[]),
            }));
          } else if (event === "step") {
            patch((message) => ({
              ...message,
              thinking: updatePlanStep(message.thinking, data as unknown as PlanStep, time),
            }));
          } else if (event === "answer") {
            patch((message) => ({
              ...message,
              state: "done",
              answer: data as unknown as AnswerPayload,
              thinking: finishThinking(message.thinking, time),
            }));
          } else if (event === "error") {
            patch((message) => ({
              ...message,
              state: "error",
              error: String(data.message ?? "处理失败"),
              thinking: finishThinking(message.thinking, time),
            }));
          }
        },
        { model, mode, history: buildHistory(base), signal: controller.signal },
      )
        .then(() => {
          // A stream that closes with no result must not stay in the loading state.
          patch((message) =>
            message.state === "streaming"
              ? {
                  ...message,
                  state: "error",
                  error: "服务端没有返回结果",
                  thinking: finishThinking(message.thinking, Date.now()),
                }
              : message,
          );
        })
        .catch((error: unknown) => {
          const stopped = controller.signal.aborted;
          patch((message) => ({
            ...message,
            state: stopped ? "stopped" : "error",
            error: stopped ? undefined : error instanceof Error ? error.message : "请求失败",
            thinking: finishThinking(message.thinking, Date.now()),
          }));
        })
        .finally(() => {
          abortRef.current = null;
          setStreamingId(null);
        });
    },
    [patchAssistant],
  );

  const send = useCallback(
    (text: string, model: string, mode: AskMode) => {
      const trimmed = text.trim();
      if (!trimmed || streamingId !== null) return;
      run(active.id, active.messages, trimmed, model, mode);
    },
    [active, run, streamingId],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);

  const regenerate = useCallback(
    (assistantId: string, model: string, mode?: AskMode) => {
      if (streamingId !== null) return;
      const index = active.messages.findIndex((message) => message.id === assistantId);
      const user = active.messages[index - 1];
      if (index < 1 || user.role !== "user") return;
      const kept = active.messages.slice(0, index - 1);
      run(active.id, kept, user.text, model, mode ?? user.mode ?? DEFAULT_MODE);
    },
    [active, run, streamingId],
  );

  const editAndResend = useCallback(
    (userId: string, text: string, model: string) => {
      const trimmed = text.trim();
      if (!trimmed || streamingId !== null) return;
      const index = active.messages.findIndex((message) => message.id === userId);
      const original = active.messages[index];
      if (original?.role !== "user") return;
      const kept = active.messages.slice(0, index);
      run(active.id, kept, trimmed, model, original.mode ?? DEFAULT_MODE);
    },
    [active, run, streamingId],
  );

  const giveFeedback = useCallback(
    async (assistantId: string, verdict: "correct" | "wrong") => {
      const message = active.messages.find((item) => item.id === assistantId);
      if (message?.role !== "assistant" || !message.answer?.log_id) return;
      await sendFeedback(message.answer.log_id, verdict);
      patchAssistant(active.id, assistantId, (current) => ({ ...current, verdict }));
    },
    [active, patchAssistant],
  );

  const newConversation = useCallback(() => {
    const empty = items.find((item) => item.messages.length === 0);
    if (empty) {
      setActiveId(empty.id);
      return;
    }
    const fresh = createConversation(Date.now());
    setItems((prev) => [fresh, ...prev]);
    setActiveId(fresh.id);
  }, [items]);

  const renameConversation = useCallback((id: string, title: string) => {
    const trimmed = title.trim();
    if (!trimmed) return;
    setItems((prev) => prev.map((item) => (item.id === id ? { ...item, title: trimmed } : item)));
  }, []);

  const deleteConversation = useCallback(
    (id: string) => {
      if (id === streamingId) abortRef.current?.abort();
      const rest = items.filter((item) => item.id !== id);
      const next = rest.length ? rest : [createConversation(Date.now())];
      setItems(next);
      if (id === activeId) setActiveId(next[0].id);
    },
    [activeId, items, streamingId],
  );

  return {
    conversations: items,
    active,
    streaming: streamingId !== null,
    send,
    stop,
    regenerate,
    editAndResend,
    giveFeedback,
    newConversation,
    selectConversation: setActiveId,
    renameConversation,
    deleteConversation,
  };
}
