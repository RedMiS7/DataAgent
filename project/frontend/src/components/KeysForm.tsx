import { useState, type FormEvent } from "react";
import { ExclamationCircleOutlined } from "@ant-design/icons";
import { Button, Input } from "antd";
import { saveKeys } from "../api";
import type { SetupStatus } from "../types";

const FIELDS = [
  {
    name: "llm",
    label: "大模型 Key",
    hint: "DeepSeek，用来理解问题和生成查询",
    href: "https://platform.deepseek.com/api_keys",
  },
  {
    name: "embedding",
    label: "向量模型 Key",
    hint: "阿里云百炼，用来查找相关字段和口径",
    href: "https://bailian.console.aliyun.com/",
  },
] as const;

interface Props {
  status: SetupStatus;
  onSaved: (status: SetupStatus) => void;
}

export default function KeysForm({ status, onSaved }: Props) {
  const [values, setValues] = useState({ llm: "", embedding: "" });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const configured = { llm: status.llm_configured, embedding: status.embedding_configured };
  const canSave = Boolean(values.llm.trim() || values.embedding.trim()) && !saving;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!canSave) return;
    setSaving(true);
    setError("");
    try {
      onSaved(await saveKeys(values.llm, values.embedding));
      setValues({ llm: "", embedding: "" });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="keys-form" onSubmit={(event) => void submit(event)}>
      {FIELDS.map((field) => (
        <div className="keys-field" key={field.name}>
          <label htmlFor={`key-${field.name}`}>{field.label}</label>
          <Input.Password
            id={`key-${field.name}`}
            autoComplete="off"
            value={values[field.name]}
            placeholder={configured[field.name] ? "已配置，留空表示不修改" : "粘贴 Key"}
            onChange={(event) => setValues({ ...values, [field.name]: event.target.value })}
          />
          <p>
            {field.hint}。
            <a href={field.href} target="_blank" rel="noreferrer">获取 Key</a>
          </p>
        </div>
      ))}
      {error && (
        <div className="notice notice-error" role="alert">
          <ExclamationCircleOutlined /> {error}
        </div>
      )}
      <Button type="primary" htmlType="submit" disabled={!canSave} loading={saving}>
        {saving ? "正在验证 Key" : "保存 Key"}
      </Button>
    </form>
  );
}
