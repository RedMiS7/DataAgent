from __future__ import annotations

import asyncio
from collections.abc import Sequence

from agentscope.embedding import EmbeddingModelBase

from .base import EmbeddingError


class ScopeEmbedder:
    """Adapts an AgentScope embedding model to the synchronous provider protocol."""

    def __init__(self, model: EmbeddingModelBase) -> None:
        self._model = model
        self.model_name = model.model
        self.dim = int(model.dimensions or 0)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = asyncio.run(self._model(list(texts)))
        except Exception as exc:
            raise EmbeddingError(f"embedding request failed: {exc}") from exc

        vectors = [[float(value) for value in vector] for vector in response.embeddings]
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"embedding count mismatch: expected {len(texts)}, got {len(vectors)}")
        if self.dim:
            for vector in vectors:
                if len(vector) != self.dim:
                    raise EmbeddingError(
                        f"embedding dimension mismatch: expected {self.dim}, got {len(vector)}")
        return vectors