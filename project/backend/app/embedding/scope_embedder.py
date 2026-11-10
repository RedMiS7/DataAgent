from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from agentscope.embedding import EmbeddingModelBase

from .base import EmbeddingError


class ScopeEmbedder:
    """Adapts an AgentScope embedding model to the synchronous provider protocol."""

    def __init__(self, factory: Callable[[], EmbeddingModelBase | None],
                 model_name: str, dim: int) -> None:
        self._factory = factory
        self.model_name = model_name
        self.dim = dim

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        from ..agent.scope_model import close_model

        model = self._factory()
        if model is None:
            raise EmbeddingError("没有配置向量模型")
        try:
            response = await model(texts)
            return [[float(value) for value in vector] for vector in response.embeddings]
        finally:
            # AgentScope batches use gather, which leaves sibling batches running
            # when one fails. This loop belongs to this embedding call only.
            pending = asyncio.all_tasks() - {asyncio.current_task()}
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await close_model(model)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            vectors = asyncio.run(self._embed(list(texts)))
        except Exception as exc:
            raise EmbeddingError(f"embedding request failed: {exc}") from exc

        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"embedding count mismatch: expected {len(texts)}, got {len(vectors)}")
        if self.dim:
            for vector in vectors:
                if len(vector) != self.dim:
                    raise EmbeddingError(
                        f"embedding dimension mismatch: expected {self.dim}, got {len(vector)}")
        return vectors
