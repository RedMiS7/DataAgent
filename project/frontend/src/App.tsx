import { Tabs, Typography } from "antd";
import AttributionPage from "./pages/AttributionPage";
import ChatPage from "./pages/ChatPage";
import ReviewPage from "./pages/ReviewPage";

const { Title, Text } = Typography;

export default function App() {
  return (
    <div className="page">
      <div className="page-header">
        <Title level={3} style={{ margin: 0 }}>
          DataAgent
        </Title>
        <Text className="subtitle">一人公司经营数据助手 · 用中文问数</Text>
      </div>
      <Tabs
        defaultActiveKey="chat"
        items={[
          { key: "chat", label: "智能问数", children: <ChatPage /> },
          { key: "attribution", label: "异动归因", children: <AttributionPage /> },
          { key: "review", label: "案例审核", children: <ReviewPage /> },
        ]}
      />
    </div>
  );
}