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

    _, model_name, dim, _ = resolve_embedding(
        provider,
        settings.embedding_endpoint,
        settings.embedding_model,
        settings.embedding_dim,
        settings.embedding_batch_size,
    )
    if not model_name:
        return None

    model = build_embedding_model(settings)
    if model is None:
        return None
    embedder = ScopeEmbedder(model)
    if dim and embedder.dim and dim != embedder.dim:
        embedder.dim = dim
    return embedder


__all__ = ["EmbeddingError", "EmbeddingProvider", "build_embedder"]