import { useMemo, useState } from "react";
import { MenuOutlined } from "@ant-design/icons";
import { App as AntApp, ConfigProvider, theme as antTheme } from "antd";
import zhCN from "antd/locale/zh_CN";
import Sidebar, { type View } from "./components/Sidebar";
import { useChat } from "./hooks/useChat";
import { useSetup } from "./hooks/useSetup";
import { ThemeContext, useTheme } from "./hooks/useTheme";
import ChatPage from "./pages/ChatPage";
import ReviewPage from "./pages/ReviewPage";
import SettingsPage from "./pages/SettingsPage";
import SetupPage from "./pages/SetupPage";

const PANE_TITLES: Record<Exclude<View, "chat">, string> = {
  review: "案例审核",
  settings: "模型设置",
};

function Shell({ theme, onToggleTheme }: { theme: "light" | "dark"; onToggleTheme: () => void }) {
  const chat = useChat();
  const [view, setView] = useState<View>("chat");
  const [sidebarOpen, setSidebarOpen] = useState(false);

  return (
    <div className="shell">
      <Sidebar
        chat={chat}
        view={view}
        theme={theme}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onView={setView}
        onToggleTheme={onToggleTheme}
      />
      <main className="main">
        {view === "chat" ? (
          <ChatPage chat={chat} onOpenSidebar={() => setSidebarOpen(true)} />
        ) : (
          <div className="pane">
            <header className="chat-top">
              <button
                type="button"
                className="icon-button menu-button"
                aria-label="打开侧栏"
                onClick={() => setSidebarOpen(true)}
              >
                <MenuOutlined />
              </button>
              <span className="chat-title">{PANE_TITLES[view]}</span>
            </header>
            <div className="pane-body">
              {view === "review" && <ReviewPage />}
              {view === "settings" && <SettingsPage />}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

export default function App() {
  const { theme, toggle } = useTheme();
  const setup = useSetup();

  const config = useMemo(
    () => ({
      algorithm: theme === "dark" ? antTheme.darkAlgorithm : antTheme.defaultAlgorithm,
      token: {
        colorPrimary: theme === "dark" ? "#30A07C" : "#0A7A5C",
        borderRadius: 8,
        fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
      },
    }),
    [theme],
  );

  return (
    <ThemeContext.Provider value={theme}>
      <ConfigProvider locale={zhCN} theme={config}>
        <AntApp>
          {setup.entered ? (
            <Shell theme={theme} onToggleTheme={toggle} />
          ) : (
            <SetupPage setup={setup} />
          )}
        </AntApp>
      </ConfigProvider>
    </ThemeContext.Provider>
  );
}
