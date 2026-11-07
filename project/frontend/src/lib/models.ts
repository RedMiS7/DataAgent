export interface ModelOption {
  id: string;
  label: string;
  description: string;
  reasoning: boolean;
}

/** Shown until the backend offers a model list. */
export const DEFAULT_MODELS: ModelOption[] = [
  {
    id: "deepseek-chat",
    label: "DeepSeek Chat",
    description: "响应快，适合日常查数",
    reasoning: false,
  },
  {
    id: "deepseek-reasoner",
    label: "DeepSeek Reasoner",
    description: "先推理再回答，适合复杂分析",
    reasoning: true,
  },
];

const STORAGE_KEY = "dataagent.model";

export function resolveModelId(models: ModelOption[], saved: string | null): string {
  const match = models.find((model) => model.id === saved);
  return (match ?? models[0]).id;
}

export function loadSavedModel(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function saveModel(id: string): void {
  try {
    localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // Storage may be blocked; the choice then lasts for this page only.
  }
}
