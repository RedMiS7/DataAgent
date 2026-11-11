from __future__ import annotations

from ..cache import caching
from ..config import Settings
from ..providers import resolve_embedding
from .base import EmbeddingError, EmbeddingProvider
from .cached import CachedEmbedder
from .fake import HashEmbedder
from .scope_embedder import ScopeEmbedder


def build_embedder(settings: Settings, *, cached: bool = True) -> EmbeddingProvider | None:
    """The configured embedding model, behind the vector cache when there is one.

    ``cached=False`` for checking a key: a cached vector would let a bad key pass.
    """
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
    embedder = ScopeEmbedder(lambda: build_embedding_model(configured), model_name, dim)
    cache = caching(settings) if cached else None
    if cache is None:
        return embedder
    return CachedEmbedder(embedder, cache, settings.embedding_cache_ttl_seconds,
                          settings.mysql_business_db)


__all__ = ["EmbeddingError", "EmbeddingProvider", "build_embedder"]
