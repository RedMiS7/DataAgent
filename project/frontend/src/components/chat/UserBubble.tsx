import { useState } from "react";
import { CopyOutlined, EditOutlined } from "@ant-design/icons";
import { Button, Input, Tooltip } from "antd";
import type { UserMessage } from "../../lib/conversations";

interface Props {
  message: UserMessage;
  disabled: boolean;
  onCopy: (text: string) => void;
  onResend: (text: string) => void;
}

export default function UserBubble({ message, disabled, onCopy, onResend }: Props) {
  const [draft, setDraft] = useState<string | null>(null);

  if (draft !== null) {
    return (
      <div className="user-edit">
        <Input.TextArea
          autoFocus
          value={draft}
          maxLength={500}
          autoSize={{ minRows: 2, maxRows: 8 }}
          aria-label="编辑问题"
          onChange={(event) => setDraft(event.target.value)}
        />
        <div className="user-edit-bar">
          <Button size="small" onClick={() => setDraft(null)}>取消</Button>
          <Button
            size="small"
            type="primary"
            disabled={!draft.trim() || disabled}
            onClick={() => {
              onResend(draft);
              setDraft(null);
            }}
          >
            重新提问
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="user-row">
      <div className="user-bubble">{message.text}</div>
      <div className="message-actions">
        <Tooltip title="复制">
          <button type="button" aria-label="复制问题" onClick={() => onCopy(message.text)}>
            <CopyOutlined />
          </button>
        </Tooltip>
        <Tooltip title="编辑后重新提问">
          <button
            type="button"
            aria-label="编辑问题"
            disabled={disabled}
            onClick={() => setDraft(message.text)}
          >
            <EditOutlined />
          </button>
        </Tooltip>
      </div>
    </div>
  );
}
