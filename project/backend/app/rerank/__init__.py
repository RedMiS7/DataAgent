from __future__ import annotations

from ..config import Settings
from ..providers import resolve_rerank
from .base import Reranker, RerankError, RerankResult
from .dashscope import DashScopeReranker
from .fake import FakeReranker


def build_reranker(settings: Settings) -> Reranker | None:
    """The configured rerank model; None when it is turned off or has no key, and
    retrieval then keeps its own order."""
    provider = settings.rerank_provider.lower()
    if provider in ("", "none", "off"):
        return None
    if provider == "fake":
        return FakeReranker()
    endpoint, model = resolve_rerank(provider, settings.rerank_endpoint, settings.rerank_model)
    # The same Bailian key serves embedding and rerank.
    api_key = settings.rerank_api_key or settings.embedding_api_key
    if not endpoint or not model or not api_key:
        return None
    return DashScopeReranker(endpoint, model, api_key, settings.rerank_timeout_seconds)


__all__ = ["RerankError", "RerankResult", "Reranker", "build_reranker"]
