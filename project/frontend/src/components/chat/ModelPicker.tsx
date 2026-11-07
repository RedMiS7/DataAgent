import { useState } from "react";
import { CheckOutlined, DownOutlined } from "@ant-design/icons";
import { Dropdown } from "antd";
import type { ModelOption } from "../../lib/models";

interface Props {
  models: ModelOption[];
  value: string;
  live: boolean;
  onChange: (id: string) => void;
}

export default function ModelPicker({ models, value, live, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const current = models.find((model) => model.id === value) ?? models[0];
  return (
    <Dropdown
      open={open}
      onOpenChange={setOpen}
      trigger={["click"]}
      placement="topLeft"
      dropdownRender={() => (
        <div className="model-menu" role="listbox" aria-label="选择模型">
          {models.map((model) => (
            <button
              key={model.id}
              type="button"
              role="option"
              aria-selected={model.id === current.id}
              className="model-option"
              onClick={() => {
                onChange(model.id);
                setOpen(false);
              }}
            >
              <span className="model-option-text">
                <span className="model-option-name">
                  {model.label}
                  {model.reasoning && <span className="pill">推理</span>}
                </span>
                <span className="model-option-desc">{model.description}</span>
              </span>
              {model.id === current.id && <CheckOutlined className="model-option-check" />}
            </button>
          ))}
          {!live && (
            <p className="model-menu-note">后端暂未接入切换，回答仍由服务端默认模型生成。</p>
          )}
        </div>
      )}
    >
      <button type="button" className="model-trigger" aria-label={`当前模型：${current.label}`}>
        {current.label}
        <DownOutlined className="model-trigger-caret" />
      </button>
    </Dropdown>
  );
}
