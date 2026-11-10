import { useEffect, useState } from "react";
import { DownOutlined } from "@ant-design/icons";
import { stepLabel, stepState } from "../../lib/plan";
import { formatSeconds, type PlanStepView, type Thinking } from "../../lib/reasoning";

const WARN_STEPS = new Set(["retrying", "declined", "plan_fallback"]);

function PlanList({ plan, running }: { plan: PlanStepView[]; running: boolean }) {
  return (
    <ol className="plan-steps" aria-label="分析计划">
      {plan.map((step) => (
        <li key={step.id} className={`plan-step is-${stepState(step.status, running)}`}>
          <span className="plan-mark" aria-hidden="true" />
          <span className="plan-goal">
            第{step.id}步：{step.goal}
            {step.origin === "supplement" && <span className="plan-tag">补查</span>}
          </span>
          <span className="plan-state">
            {stepLabel(step.status, running)}
            {step.startedAt !== undefined && step.endedAt !== undefined
              && ` · ${formatSeconds(step.endedAt - step.startedAt)}`}
          </span>
        </li>
      ))}
    </ol>
  );
}

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
  // Plan steps run at once, so the gap between two status lines is no step's duration.
  const planned = Boolean(thinking.plan?.length);
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
          {planned && thinking.plan && <PlanList plan={thinking.plan} running={running} />}
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
                  {step.endedAt && !planned && (
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
