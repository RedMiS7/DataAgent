from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class VectorStoreError(RuntimeError):
    pass


@dataclass(slots=True)
class VectorRecord:
    id: str
    vector: list[float]
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class VectorHit:
    id: str
    score: float
    payload: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class VectorStore(Protocol):
    def ensure_collection(self, name: str, dim: int) -> None: ...

    def recreate_collection(self, name: str, dim: int) -> None: ...

    def upsert(self, name: str, records: Sequence[VectorRecord]) -> None: ...

    def search(self, name: str, vector: list[float], top_k: int,
               filters: dict[str, object] | None = None) -> list[VectorHit]: ...

    def count(self, name: str) -> int: ...

    def ping(self) -> bool: ...