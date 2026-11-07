import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ArrowUpOutlined, PauseOutlined } from "@ant-design/icons";
import { Input, Tooltip } from "antd";
import type { TextAreaRef } from "antd/es/input/TextArea";
import type { ModelOption } from "../../lib/models";
import { MODE_OPTIONS, type AskMode } from "../../lib/modes";
import ModelPicker from "./ModelPicker";

const MAX_LENGTH = 500;
const PLACEHOLDERS: Record<AskMode, string> = {
  auto: "问问你的生意，例如：上个月 GMV 是多少？最近生意怎么样？",
  query: "问问你的生意，例如：上个月哪个商品退货率最高？",
  analysis: "说说想分析什么，例如：分析一下最近30天的销量",
};

interface Props {
  streaming: boolean;
  models: ModelOption[];
  model: string;
  modelsLive: boolean;
  mode: AskMode;
  focusKey: string;
  onModelChange: (id: string) => void;
  onModeChange: (mode: AskMode) => void;
  onSend: (text: string) => void;
  onStop: () => void;
}

export default function Composer({
  streaming, models, model, modelsLive, mode, focusKey, onModelChange, onModeChange, onSend, onStop,
}: Props) {
  const [text, setText] = useState("");
  const inputRef = useRef<TextAreaRef>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, [focusKey]);

  const canSend = text.trim().length > 0 && !streaming;

  const submit = () => {
    if (!canSend) return;
    onSend(text);
    setText("");
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter during IME composition confirms a Chinese candidate; it must not send.
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    submit();
  };

  return (
    <div className="composer-wrap">
      <div className="composer">
        <Input.TextArea
          ref={inputRef}
          value={text}
          variant="borderless"
          maxLength={MAX_LENGTH}
          autoSize={{ minRows: 1, maxRows: 8 }}
          placeholder={PLACEHOLDERS[mode]}
          aria-label="输入问题"
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <div className="composer-bar">
          <div className="composer-left">
            <div className="mode-switch" role="group" aria-label="回答方式">
              {MODE_OPTIONS.map((option) => (
                <Tooltip title={option.description} key={option.id}>
                  <button
                    type="button"
                    aria-pressed={mode === option.id}
                    className={mode === option.id ? "active" : undefined}
                    onClick={() => onModeChange(option.id)}
                  >
                    {option.label}
                  </button>
                </Tooltip>
              ))}
            </div>
            <ModelPicker models={models} value={model} live={modelsLive} onChange={onModelChange} />
          </div>
          <div className="composer-right">
            {text.length > MAX_LENGTH - 100 && (
              <span className="composer-count">{text.length}/{MAX_LENGTH}</span>
            )}
            {streaming ? (
              <Tooltip title="停止生成">
                <button type="button" className="send-button stop" aria-label="停止生成" onClick={onStop}>
                  <PauseOutlined />
                </button>
              </Tooltip>
            ) : (
              <button
                type="button"
                className="send-button"
                aria-label="发送"
                disabled={!canSend}
                onClick={submit}
              >
                <ArrowUpOutlined />
              </button>
            )}
          </div>
        </div>
      </div>
      <p className="composer-hint">Enter 发送，Shift + Enter 换行。结论由 AI 生成，请结合口径核对。</p>
    </div>
  );
}
