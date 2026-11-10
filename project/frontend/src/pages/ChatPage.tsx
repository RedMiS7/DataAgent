import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowDownOutlined, MenuOutlined } from "@ant-design/icons";
import { Alert, App as AntApp } from "antd";
import { fetchMeta, fetchModels } from "../api";
import AnswerCard from "../components/chat/AnswerCard";
import Composer from "../components/chat/Composer";
import EmptyState from "../components/chat/EmptyState";
import ReasoningPanel from "../components/chat/ReasoningPanel";
import UserBubble from "../components/chat/UserBubble";
import type { ChatApi } from "../hooks/useChat";
import {
  DEFAULT_MODELS,
  loadSavedModel,
  resolveModelId,
  saveModel,
  type ModelOption,
} from "../lib/models";
import { loadSavedMode, saveMode, type AskMode } from "../lib/modes";
import type { MetaInfo } from "../types";

const FOLLOW_THRESHOLD = 120;

interface Props {
  chat: ChatApi;
  onOpenSidebar: () => void;
}

export default function ChatPage({ chat, onOpenSidebar }: Props) {
  const { message } = AntApp.useApp();
  const [models, setModels] = useState<ModelOption[]>(DEFAULT_MODELS);
  const [modelsLive, setModelsLive] = useState(false);
  const [model, setModel] = useState(() => resolveModelId(DEFAULT_MODELS, loadSavedModel()));
  const [mode, setMode] = useState<AskMode>(loadSavedMode);
  const [meta, setMeta] = useState<MetaInfo | null>(null);
  const [atBottom, setAtBottom] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const landedRef = useRef<string | null>(null);

  const { active, streaming } = chat;
  const messages = active.messages;

  useEffect(() => {
    void fetchModels().then((list) => {
      setModels(list.models);
      setModelsLive(list.live);
      setModel(resolveModelId(list.models, loadSavedModel()));
    });
    fetchMeta().then(setMeta).catch(() => setMeta(null));
  }, []);

  const scrollToBottom = useCallback((smooth: boolean) => {
    const box = scrollRef.current;
    if (box) box.scrollTo({ top: box.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }, []);

  useEffect(() => {
    followRef.current = true;
    setAtBottom(true);
    scrollToBottom(false);
  }, [active.id, scrollToBottom]);

  useEffect(() => {
    const last = messages[messages.length - 1];
    const finished = last?.role === "assistant" && last.state !== "streaming";
    if (finished && last.id !== landedRef.current) {
      // Answers arrive whole and can be taller than the screen, so stop at their
      // start: the conclusion is what the reader needs first.
      landedRef.current = last.id;
      const box = scrollRef.current;
      const row = box?.querySelector<HTMLElement>(`[data-message="${last.id}"]`);
      if (box && row && followRef.current) {
        followRef.current = false;
        box.scrollTo({ top: row.offsetTop - 12 });
        return;
      }
    }
    if (followRef.current) scrollToBottom(false);
  }, [messages, scrollToBottom]);

  const onScroll = () => {
    const box = scrollRef.current;
    if (!box) return;
    const near = box.scrollHeight - box.scrollTop - box.clientHeight < FOLLOW_THRESHOLD;
    followRef.current = near;
    setAtBottom(near);
  };

  const send = (text: string) => {
    followRef.current = true;
    chat.send(text, model, mode);
  };

  const copy = (text: string) => {
    navigator.clipboard.writeText(text).then(
      () => message.success("已复制"),
      () => message.error("复制失败，请手动选择文本"),
    );
  };

  const feedback = (id: string, verdict: "correct" | "wrong", analysis: boolean) => {
    const saved = analysis ? "已记录" : "已记录，进入待审核列表";
    chat.giveFeedback(id, verdict).then(
      () => message.success(verdict === "correct" ? saved : "已记录为错误"),
      (error: unknown) => message.error(error instanceof Error ? error.message : "反馈失败"),
    );
  };

  const lastAssistantId = [...messages].reverse().find((item) => item.role === "assistant")?.id;

  return (
    <div className="chat">
      <header className="chat-top">
        <button type="button" className="icon-button menu-button" aria-label="打开侧栏" onClick={onOpenSidebar}>
          <MenuOutlined />
        </button>
        <span className="chat-title">{active.title}</span>
        {meta && <span className="chat-asof">数据截止 {meta.as_of_date}</span>}
      </header>

      {meta && !meta.llm_configured && (
        <Alert
          banner
          type="warning"
          message="服务端未配置大模型，提问会失败。请在 project/.env 填写 LLM_API_KEY 后重启后端。"
        />
      )}

      <div className="chat-scroll" ref={scrollRef} onScroll={onScroll}>
        <div className="chat-column">
          {messages.length === 0 && <EmptyState asOfDate={meta?.as_of_date} onPick={send} />}
          {messages.map((item) =>
            item.role === "user" ? (
              <UserBubble
                key={item.id}
                message={item}
                disabled={streaming}
                onCopy={copy}
                onResend={(text) => chat.editAndResend(item.id, text, model)}
              />
            ) : (
              <div className="assistant-row" key={item.id} data-message={item.id}>
                <ReasoningPanel thinking={item.thinking} running={item.state === "streaming"} />
                <AnswerCard
                  message={item}
                  canRegenerate={item.id === lastAssistantId && !streaming}
                  onRegenerate={() => chat.regenerate(item.id, model)}
                  onReanalyze={() => {
                    setMode("analysis");
                    saveMode("analysis");
                    chat.regenerate(item.id, model, "analysis");
                  }}
                  onFeedback={(verdict) => feedback(item.id, verdict, item.answer?.mode === "analysis")}
                  onCopy={copy}
                  onPick={send}
                />
              </div>
            ),
          )}
        </div>
      </div>

      <div className="chat-dock">
        {!atBottom && (
          <button
            type="button"
            className="to-bottom"
            aria-label="回到底部"
            onClick={() => scrollToBottom(true)}
          >
            <ArrowDownOutlined />
          </button>
        )}
        <Composer
          streaming={streaming}
          models={models}
          model={model}
          modelsLive={modelsLive}
          mode={mode}
          focusKey={active.id}
          onModelChange={(id) => {
            setModel(id);
            saveModel(id);
          }}
          onModeChange={(next) => {
            setMode(next);
            saveMode(next);
          }}
          onSend={send}
          onStop={chat.stop}
        />
      </div>
    </div>
  );
}
