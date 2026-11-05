from __future__ import annotations

from ..config import Settings
from .base import VectorHit, VectorRecord, VectorStore, VectorStoreError
from .memory_store import MemoryVectorStore
from .qdrant_store import QdrantStore


def build_vector_store(settings: Settings) -> VectorStore | None:
    store = QdrantStore(settings.qdrant_url)
    return store if store.ping() else None


__all__ = [
    "MemoryVectorStore",
    "QdrantStore",
    "VectorHit",
    "VectorRecord",
    "VectorStore",
    "VectorStoreError",
    "build_vector_store",
]