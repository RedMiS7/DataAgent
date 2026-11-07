import { Button } from "antd";
import KeysForm from "../components/KeysForm";
import type { SetupApi } from "../hooks/useSetup";
import { needsKeys, setupHeading, stepResult } from "../lib/setup";

export default function SetupPage({ setup }: { setup: SetupApi }) {
  const { status, unreachable } = setup;

  if (!status) {
    return (
      <main className="setup">
        <p className="setup-brand">DataAgent</p>
        <h1>{unreachable ? "连不上后端服务" : "正在连接"}</h1>
        {unreachable && <p className="setup-lead">确认后端已启动，这个页面会自动重试。</p>}
      </main>
    );
  }

  return (
    <main className="setup">
      <p className="setup-brand">DataAgent</p>
      <h1>{setupHeading(status)}</h1>
      <p className="setup-lead">
        {needsKeys(status)
          ? "Key 只保存在这台电脑上，保存后不能在页面上查看。"
          : "完成后会自动进入，不用刷新页面。"}
      </p>

      {needsKeys(status) && <KeysForm status={status} onSaved={setup.update} />}

      <ol className="ledger" aria-label="准备进度">
        {status.steps.map((step) => (
          <li key={step.key} className={`ledger-row is-${step.state}`}>
            <span>{step.label}</span>
            <span className="ledger-leader" aria-hidden="true" />
            <span className="ledger-result">{stepResult(step, status)}</span>
          </li>
        ))}
      </ol>

      {status.error && (
        <div className="setup-error" role="alert">
          <p>{status.error}</p>
          <Button onClick={setup.retry}>重新准备</Button>
        </div>
      )}
    </main>
  );
}
