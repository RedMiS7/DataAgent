"""A reranker for tests and offline use: scores by shared character pairs."""

from __future__ import annotations

from collections.abc import Sequence

from .base import RerankResult


def _pairs(text: str) -> set[str]:
    return {text[index:index + 2] for index in range(len(text) - 1)}


class FakeReranker:
    model_name = "fake-rerank"

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankResult]:
        wanted = _pairs(query)
        scores = [len(wanted & _pairs(document)) / (len(wanted) or 1) for document in documents]
        order = sorted(range(len(documents)), key=lambda index: (-scores[index], index))
        return [RerankResult(index=index, score=scores[index]) for index in order[:top_n]]
