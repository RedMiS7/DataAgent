import { useMemo, useState } from "react";
import {
  AuditOutlined,
  CloseOutlined,
  DeleteOutlined,
  EditOutlined,
  MessageOutlined,
  MoonOutlined,
  MoreOutlined,
  PlusOutlined,
  SearchOutlined,
  SettingOutlined,
  SunOutlined,
} from "@ant-design/icons";
import { App as AntApp, Dropdown, Input } from "antd";
import type { ChatApi } from "../hooks/useChat";
import type { Theme } from "../hooks/useTheme";
import { groupConversations, searchConversations, type Conversation } from "../lib/conversations";

export type View = "chat" | "review" | "settings";

const NAV: { key: View; label: string; icon: JSX.Element }[] = [
  { key: "chat", label: "智能问数", icon: <MessageOutlined /> },
  { key: "review", label: "案例审核", icon: <AuditOutlined /> },
  { key: "settings", label: "模型设置", icon: <SettingOutlined /> },
];

interface RowProps {
  item: Conversation;
  active: boolean;
  onOpen: () => void;
  onRename: (title: string) => void;
  onDelete: () => void;
}

function ConversationRow({ item, active, onOpen, onRename, onDelete }: RowProps) {
  const { modal } = AntApp.useApp();
  const [draft, setDraft] = useState<string | null>(null);

  if (draft !== null) {
    const commit = () => {
      onRename(draft);
      setDraft(null);
    };
    return (
      <Input
        autoFocus
        size="small"
        value={draft}
        maxLength={40}
        aria-label="重命名对话"
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onPressEnter={commit}
        onKeyDown={(event) => event.key === "Escape" && setDraft(null)}
      />
    );
  }

  return (
    <div className={`conv-row${active ? " active" : ""}`}>
      <button type="button" className="conv-title" onClick={onOpen}>{item.title}</button>
      <Dropdown
        trigger={["click"]}
        menu={{
          items: [
            { key: "rename", label: "重命名", icon: <EditOutlined /> },
            { key: "delete", label: "删除", icon: <DeleteOutlined />, danger: true },
          ],
          onClick: ({ key }) => {
            if (key === "rename") setDraft(item.title);
            else {
              modal.confirm({
                title: "删除这个对话？",
                content: "只删除本机保存的记录，无法恢复。",
                okText: "删除",
                okButtonProps: { danger: true },
                cancelText: "取消",
                onOk: onDelete,
              });
            }
          },
        }}
      >
        <button type="button" className="conv-more" aria-label={`${item.title}的更多操作`}>
          <MoreOutlined />
        </button>
      </Dropdown>
    </div>
  );
}

interface Props {
  chat: ChatApi;
  view: View;
  theme: Theme;
  open: boolean;
  onClose: () => void;
  onView: (view: View) => void;
  onToggleTheme: () => void;
}

export default function Sidebar({ chat, view, theme, open, onClose, onView, onToggleTheme }: Props) {
  const [keyword, setKeyword] = useState("");
  const groups = useMemo(() => {
    const started = chat.conversations.filter((item) => item.messages.length > 0);
    return groupConversations(searchConversations(started, keyword), Date.now());
  }, [chat.conversations, keyword]);

  const openChat = (action: () => void) => {
    action();
    onView("chat");
    onClose();
  };

  return (
    <>
      {open && <button type="button" className="scrim" aria-label="关闭侧栏" onClick={onClose} />}
      <aside className={`sidebar${open ? " open" : ""}`}>
        <div className="brand">
          <span className="brand-name">DataAgent</span>
          <span className="brand-sub">经营数据助手</span>
          <button type="button" className="icon-button sidebar-close" aria-label="关闭侧栏" onClick={onClose}>
            <CloseOutlined />
          </button>
        </div>

        <button type="button" className="new-chat" onClick={() => openChat(chat.newConversation)}>
          <PlusOutlined /> 新对话
        </button>

        <Input
          allowClear
          size="small"
          className="conv-search"
          prefix={<SearchOutlined />}
          placeholder="搜索对话"
          aria-label="搜索对话"
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
        />

        <div className="conv-list">
          {groups.map((group) => (
            <section key={group.label}>
              <h3>{group.label}</h3>
              {group.items.map((item) => (
                <ConversationRow
                  key={item.id}
                  item={item}
                  active={view === "chat" && item.id === chat.active.id}
                  onOpen={() => openChat(() => chat.selectConversation(item.id))}
                  onRename={(title) => chat.renameConversation(item.id, title)}
                  onDelete={() => chat.deleteConversation(item.id)}
                />
              ))}
            </section>
          ))}
          {groups.length === 0 && (
            <p className="conv-empty">{keyword ? "没有匹配的对话" : "还没有对话记录"}</p>
          )}
        </div>

        <nav className="side-nav" aria-label="功能">
          {NAV.map((item) => (
            <button
              key={item.key}
              type="button"
              className={view === item.key ? "active" : undefined}
              aria-current={view === item.key ? "page" : undefined}
              onClick={() => {
                onView(item.key);
                onClose();
              }}
            >
              {item.icon} {item.label}
            </button>
          ))}
          <button type="button" onClick={onToggleTheme}>
            {theme === "dark" ? <SunOutlined /> : <MoonOutlined />}
            {theme === "dark" ? " 浅色模式" : " 深色模式"}
          </button>
        </nav>
      </aside>
    </>
  );
}
