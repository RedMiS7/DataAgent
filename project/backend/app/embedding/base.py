from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


class EmbeddingError(RuntimeError):
    pass


@runtime_checkable
class EmbeddingProvider(Protocol):
    model_name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...