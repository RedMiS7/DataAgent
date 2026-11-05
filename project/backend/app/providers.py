"""Known OpenAI-compatible providers and their default settings.

Leave ``LLM_ENDPOINT`` / ``LLM_MODEL`` empty to use these presets, or fill them
explicitly to override. Both services expose OpenAI-compatible endpoints, so no
provider-specific client code is needed.
"""

from __future__ import annotations

LLM_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "maas": {},
}

EMBEDDING_PRESETS: dict[str, dict[str, object]] = {
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "text-embedding-v3",
        "dim": 1024,
        "batch_size": 10,
    },
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "text-embedding-v3",
        "dim": 1024,
        "batch_size": 10,
    },
    "maas": {},
}


def resolve_llm(provider: str, endpoint: str, model: str) -> tuple[str, str]:
    preset = LLM_PRESETS.get(provider.lower(), {})
    return (
        endpoint.strip() or str(preset.get("base_url", "")),
        model.strip() or str(preset.get("model", "")),
    )


def resolve_embedding(provider: str, endpoint: str, model: str, dim: int,
                      batch_size: int) -> tuple[str, str, int, int]:
    preset = EMBEDDING_PRESETS.get(provider.lower(), {})
    resolved_endpoint = endpoint.strip() or str(preset.get("base_url", ""))
    resolved_model = model.strip() or str(preset.get("model", ""))
    preset_dim = int(preset.get("dim", 0))
    resolved_dim = dim or preset_dim or 1024
    preset_batch = int(preset.get("batch_size", 0))
    resolved_batch = batch_size
    if preset_batch and (resolved_batch <= 0 or resolved_batch > preset_batch):
        resolved_batch = preset_batch
    return resolved_endpoint, resolved_model, resolved_dim, max(1, resolved_batch)