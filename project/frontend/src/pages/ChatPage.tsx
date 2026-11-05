import { useState } from "react";
import {
  Alert,
  App as AntApp,
  Button,
  Collapse,
  Input,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import { askStream, sendFeedback } from "../api";
import ChartView from "../components/ChartView";
import ResultTable from "../components/ResultTable";
import type { AnswerPayload } from "../types";

const { Text } = Typography;

interface ChatMessage {
  id: string;
  question: string;
  answer?: AnswerPayload;
  error?: string;
}

interface StatusLine {
  step: string;
  message: string;
}

const EXAMPLES = [
  "上个月哪个商品退货率最高？",
  "最近 30 天 GMV 的趋势如何？",
  "哪个投放渠道的 ROI 最低？",
  "哪个省份的发货时效最慢？",
];

function newSessionId(): string {
  return `session-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
}

export default function ChatPage() {
  const { message } = AntApp.useApp();
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sessionId] = useState(newSessionId);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [statusLines, setStatusLines] = useState<StatusLine[]>([]);

  const submit = async (question: string) => {
    const trimmed = question.trim();
    if (!trimmed || loading) return;
    const id = `m-${Date.now()}`;
    setMessages((prev) => [...prev, { id, question: trimmed }]);
    setStatusLines([]);
    setInput("");
    setLoading(true);

    const update = (patch: Partial<ChatMessage>) => {
      setMessages((prev) => prev.map((item) => (item.id === id ? { ...item, ...patch } : item)));
    };

    try {
      await askStream(trimmed, sessionId, (event, data) => {
        if (event === "status") {
          const step = String(data.step ?? "");
          const text = String(data.message ?? "");
          setStatusLines((prev) => {
            if (prev.length && prev[prev.length - 1].step === step) {
              return [...prev.slice(0, -1), { step, message: text }];
            }
            return [...prev, { step, message: text }];
          });
        } else if (event === "answer") {
          update({ answer: data as unknown as AnswerPayload });
        } else if (event === "error") {
          update({ error: String(data.message ?? "处理失败") });
        }
      });
    } catch (error) {
      update({ error: error instanceof Error ? error.message : "请求失败" });
    } finally {
      setLoading(false);
    }
  };

  const feedback = async (logId: number | null, verdict: "correct" | "wrong") => {
    if (!logId) return;
    try {
      await sendFeedback(logId, verdict);
      message.success(verdict === "correct" ? "已记录为正确，进入待审核列表" : "已记录为错误");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "反馈失败");
    }
  };

  return (
    <div className="chat-layout">
      <div>
        <div className="ask-box">
          <Input.TextArea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="用中文提问，例如：上个月哪个商品退货率最高？"
            autoSize={{ minRows: 2, maxRows: 5 }}
            onPressEnter={(event) => {
              if (!event.shiftKey) {
                event.preventDefault();
                void submit(input);
              }
            }}
          />
          <Space style={{ marginTop: 12 }} wrap>
            <Button type="primary" loading={loading} onClick={() => void submit(input)}>
              提问
            </Button>
            {EXAMPLES.map((example) => (
              <Tag
                key={example}
                style={{ cursor: "pointer" }}
                onClick={() => {
                  setInput(example);
                  void submit(example);
                }}
              >
                {example}
              </Tag>
            ))}
          </Space>
        </div>

        <div className="message-list">
          {messages.map((item) => (
            <div key={item.id}>
              <div className="bubble user">
                <div className="label">你的问题</div>
                {item.question}
              </div>
              <div className="bubble">
                <div className="label">DataAgent</div>
                {item.error && <Alert type="warning" showIcon message={item.error} />}
                {item.answer && <AnswerCard answer={item.answer} onFeedback={feedback} />}
                {!item.answer && !item.error && <Spin size="small" />}
              </div>
            </div>
          ))}
        </div>
      </div>

      <aside className="status-panel">
        <h3>处理状态</h3>
        {statusLines.length === 0 && <Text type="secondary">等待提问</Text>}
        {statusLines.map((line, index) => (
          <div key={`${line.step}-${index}`} className={index === statusLines.length - 1 ? "status-line" : "status-line muted"}>
            <span className="dot" />
            <span>{line.message}</span>
          </div>
        ))}
      </aside>
    </div>
  );
}

interface AnswerCardProps {
  answer: AnswerPayload;
  onFeedback: (logId: number | null, verdict: "correct" | "wrong") => void;
}

function AnswerCard({ answer, onFeedback }: AnswerCardProps) {
  const retrieval = answer.retrieval;
  return (
    <div>
      <div className="summary">{answer.summary}</div>
      <div className="meta-grid">
        <div>
          时间范围：<strong>{answer.time_range.label ?? ""}</strong>（{answer.time_range.start} 至{" "}
          {answer.time_range.end_exclusive}，不含右端）
        </div>
        <div>
          数据表：<strong>{answer.tables_used.join("、") || "退答"}</strong>
        </div>
        {answer.rewritten_question && answer.rewritten_question !== answer.question && (
          <div>
            改写问题：<strong>{answer.rewritten_question}</strong>
          </div>
        )}
        {answer.degraded && <div className="tag-note">检索降级：{retrieval.degraded_reason}</div>}
        {answer.retries > 0 && <div>重试次数：{answer.retries}</div>}
      </div>

      {answer.table && (
        <div className="answer-block">
          <ResultTable table={answer.table} />
        </div>
      )}

      {answer.table && answer.chart && (
        <div className="answer-block">
          <ChartView chart={answer.chart} table={answer.table} />
        </div>
      )}

      {answer.metrics.length > 0 && (
        <div className="answer-block">
          <Text strong>口径说明</Text>
          <ul style={{ margin: "8px 0 0", paddingLeft: 20, fontSize: 13, color: "#4b5563" }}>
            {answer.metrics.map((metric) => (
              <li key={metric.name}>
                <strong>{metric.name}</strong>：{metric.definition}
                {metric.formula && <span className="mono">（{metric.formula}）</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="answer-block">
        <Collapse
          ghost
          items={[
            {
              key: "sql",
              label: "查看 SQL",
              children: <div className="mono">{answer.sql || "无"}</div>,
            },
            {
              key: "retrieval",
              label: "查看检索内容",
              children: (
                <div style={{ fontSize: 13 }}>
                  <div>字段：{retrieval.schema_items.map((item) => `${item.table}.${item.column}`).join("、") || "无"}</div>
                  <div>指标：{retrieval.metrics.map((item) => item.name).join("、") || "无"}</div>
                  <div>实体：{retrieval.entities.map((item) => item.value).join("、") || "无"}</div>
                  <div>案例：{retrieval.cases.map((item) => item.question).join("；") || "无"}</div>
                </div>
              ),
            },
            {
              key: "time",
              label: "查看时间解析",
              children: <div className="mono">{JSON.stringify(answer.time_range, null, 2)}</div>,
            },
          ]}
        />
      </div>

      <div className="feedback-row">
        <Text type="secondary">这个回答对吗？</Text>
        <Button size="small" onClick={() => onFeedback(answer.log_id, "correct")}>
          正确
        </Button>
        <Button size="small" danger onClick={() => onFeedback(answer.log_id, "wrong")}>
          错误
        </Button>
        {answer.summary_source === "template" && (
          <span className="tag-note">结论由模板生成</span>
        )}
      </div>
    </div>
  );
}