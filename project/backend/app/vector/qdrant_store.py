from __future__ import annotations

import os
import uuid
from collections.abc import Sequence

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from .base import VectorHit, VectorRecord, VectorStoreError

_NAMESPACE = uuid.UUID("7b0d7f4e-9a4a-4f2a-9c1e-2f5f3d6c8b11")
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _bypass_proxy_for_loopback() -> None:
    current = os.environ.get("NO_PROXY", "")
    parts = [part.strip() for part in current.split(",") if part.strip()]
    for host in _LOOPBACK_HOSTS:
        if host not in parts:
            parts.append(host)
    os.environ["NO_PROXY"] = ",".join(parts)
    os.environ["no_proxy"] = os.environ["NO_PROXY"]


def _point_id(record_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, record_id))


class QdrantStore:
    def __init__(self, url: str, timeout: float = 5.0) -> None:
        _bypass_proxy_for_loopback()
        self.client = QdrantClient(url=url, timeout=timeout)

    def ping(self) -> bool:
        try:
            self.client.get_collections()
            return True
        except Exception:
            return False

    def ensure_collection(self, name: str, dim: int) -> None:
        try:
            if self.client.collection_exists(name):
                return
            self.client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
        except Exception as exc:
            raise VectorStoreError(f"ensure collection {name} failed: {exc}") from exc

    def recreate_collection(self, name: str, dim: int) -> None:
        try:
            if self.client.collection_exists(name):
                self.client.delete_collection(name)
            self.client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
        except Exception as exc:
            raise VectorStoreError(f"recreate collection {name} failed: {exc}") from exc

    def upsert(self, name: str, records: Sequence[VectorRecord]) -> None:
        if not records:
            return
        points = [
            PointStruct(id=_point_id(record.id), vector=record.vector,
                        payload={**record.payload, "original_id": record.id})
            for record in records
        ]
        try:
            self.client.upsert(collection_name=name, points=points)
        except Exception as exc:
            raise VectorStoreError(f"upsert into {name} failed: {exc}") from exc

    def search(self, name: str, vector: list[float], top_k: int,
               filters: dict[str, object] | None = None) -> list[VectorHit]:
        query_filter = None
        if filters:
            query_filter = Filter(must=[
                FieldCondition(key=key, match=MatchValue(value=value))
                for key, value in filters.items()
            ])
        try:
            hits = self.client.search(
                collection_name=name, query_vector=vector, limit=top_k,
                query_filter=query_filter, with_payload=True,
            )
        except Exception as exc:
            raise VectorStoreError(f"search {name} failed: {exc}") from exc
        results: list[VectorHit] = []
        for hit in hits:
            payload = dict(hit.payload or {})
            original_id = str(payload.pop("original_id", hit.id))
            results.append(VectorHit(id=original_id, score=float(hit.score), payload=payload))
        return results

    def count(self, name: str) -> int:
        try:
            if not self.client.collection_exists(name):
                return 0
            return int(self.client.count(name).count)
        except Exception:
            return 0