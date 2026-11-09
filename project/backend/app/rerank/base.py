from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class RerankError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RerankResult:
    # Position of the document in the list that was sent.
    index: int
    # Relevance within this request only; not comparable across requests.
    score: float


@runtime_checkable
class Reranker(Protocol):
    model_name: str

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankResult]:
        """The ``top_n`` most relevant documents, most relevant first."""
        ...
