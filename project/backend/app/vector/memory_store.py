from __future__ import annotations

import math
from collections.abc import Sequence

from .base import VectorHit, VectorRecord


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm_left = math.sqrt(sum(a * a for a in left)) or 1.0
    norm_right = math.sqrt(sum(b * b for b in right)) or 1.0
    return dot / (norm_left * norm_right)


class MemoryVectorStore:
    """In-memory store used by tests and offline demos."""

    def __init__(self) -> None:
        self._collections: dict[str, list[VectorRecord]] = {}

    def ping(self) -> bool:
        return True

    def ensure_collection(self, name: str, dim: int) -> None:
        self._collections.setdefault(name, [])

    def recreate_collection(self, name: str, dim: int) -> None:
        self._collections[name] = []

    def upsert(self, name: str, records: Sequence[VectorRecord]) -> None:
        self._collections.setdefault(name, [])
        by_id = {record.id: record for record in self._collections[name]}
        for record in records:
            by_id[record.id] = record
        self._collections[name] = list(by_id.values())

    def search(self, name: str, vector: list[float], top_k: int,
               filters: dict[str, object] | None = None) -> list[VectorHit]:
        hits: list[VectorHit] = []
        for record in self._collections.get(name, []):
            if filters and any(record.payload.get(k) != v for k, v in filters.items()):
                continue
            hits.append(VectorHit(id=record.id, score=_cosine(vector, record.vector),
                                  payload=dict(record.payload)))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:top_k]

    def count(self, name: str) -> int:
        return len(self._collections.get(name, []))