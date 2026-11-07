import { useEffect, useState } from "react";
import { DownOutlined } from "@ant-design/icons";
import { formatSeconds, type Thinking } from "../../lib/reasoning";

const WARN_STEPS = new Set(["retrying", "summary_fallback", "declined"]);

interface Props {
  thinking: Thinking;
  running: boolean;
}

export default function ReasoningPanel({ thinking, running }: Props) {
  const [toggled, setToggled] = useState<boolean | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 100);
    return () => window.clearInterval(timer);
  }, [running]);

  const open = toggled ?? running;
  const elapsed = (thinking.endedAt ?? now) - thinking.startedAt;
  const stepCount = thinking.steps.length;
  const queryCount = new Set(
    thinking.steps.filter((item) => item.step.startsWith("query_")).map((item) => item.step),
  ).size;

  if (!running && stepCount === 0 && !thinking.text) return null;

  return (
    <section className={`thinking${running ? " is-running" : ""}`}>
      <button
        type="button"
        className="thinking-head"
        aria-expanded={open}
        onClick={() => setToggled(!open)}
      >
        <span className="thinking-dot" />
        <span>{running ? "正在思考" : "思考过程"}</span>
        <span className="thinking-meta">
          {formatSeconds(elapsed)}
          {queryCount > 0 ? ` · ${queryCount} 条查询` : stepCount > 0 && ` · ${stepCount} 步`}
        </span>
        <DownOutlined className={`thinking-caret${open ? " open" : ""}`} />
      </button>

      {open && (
        <div className="thinking-body">
          <ol className="thinking-steps">
            {thinking.steps.map((step, index) => {
              const live = running && index === stepCount - 1;
              const warn = WARN_STEPS.has(step.step);
              return (
                <li
                  key={`${step.step}-${step.startedAt}`}
                  className={`thinking-step${live ? " live" : ""}${warn ? " warn" : ""}`}
                >
                  <span className="thinking-step-text">{step.message}</span>
                  {step.endedAt && (
                    <span className="thinking-step-time">
                      {formatSeconds(step.endedAt - step.startedAt)}
                    </span>
                  )}
                </li>
              );
            })}
          </ol>
          {thinking.text && <div className="thinking-text">{thinking.text}</div>}
        </div>
      )}
    </section>
  );
}
