from __future__ import annotations

from ..config import Settings
from ..providers import resolve_embedding
from .base import EmbeddingError, EmbeddingProvider
from .fake import HashEmbedder
from .scope_embedder import ScopeEmbedder


def build_embedder(settings: Settings) -> EmbeddingProvider | None:
    from ..agent.scope_model import build_embedding_model

    provider = settings.embedding_provider.lower()
    if provider == "fake":
        return HashEmbedder(dim=settings.embedding_dim)

    endpoint, model_name, dim, _ = resolve_embedding(
        provider,
        settings.embedding_endpoint,
        settings.embedding_model,
        settings.embedding_dim,
        settings.embedding_batch_size,
    )
    if not endpoint or not model_name or not settings.embedding_api_key.strip():
        return None

    configured = settings.model_copy()
    return ScopeEmbedder(lambda: build_embedding_model(configured), model_name, dim)


__all__ = ["EmbeddingError", "EmbeddingProvider", "build_embedder"]
