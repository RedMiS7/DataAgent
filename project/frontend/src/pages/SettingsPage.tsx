import { useEffect, useState } from "react";
import { App as AntApp } from "antd";
import { fetchSetupStatus } from "../api";
import KeysForm from "../components/KeysForm";
import type { SetupStatus } from "../types";

export default function SettingsPage() {
  const { message } = AntApp.useApp();
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    fetchSetupStatus().then(setStatus, (reason: unknown) => {
      setError(reason instanceof Error ? reason.message : "读取设置失败");
    });
  }, []);

  return (
    <div className="settings">
      <p className="settings-lead">修改后立即生效。Key 只保存在这台电脑上，保存后不能在页面上查看。</p>
      {error && <div className="notice notice-error">{error}</div>}
      {status && (
        <KeysForm
          status={status}
          onSaved={(next) => {
            setStatus(next);
            message.success("Key 已保存");
          }}
        />
      )}
    </div>
  );
}
