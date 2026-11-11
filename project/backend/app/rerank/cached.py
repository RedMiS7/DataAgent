from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from ..cache import Cache, record_lookup
from ..cache.base import KEY_PREFIX, digest, jittered
from .base import Reranker, RerankResult

logger = logging.getLogger(__name__)


class CachedReranker:
    """A reranker that reuses the result for the same question and the same documents
    in the same order. Documents come from the semantic layer, so editing it changes
    the key."""

    def __init__(self, inner: Reranker, cache: Cache, ttl_seconds: int,
                 namespace: str) -> None:
        self.inner = inner
        self.cache = cache
        self.ttl_seconds = ttl_seconds
        self.namespace = namespace
        self.model_name = inner.model_name

    def _key(self, query: str, documents: Sequence[str], top_n: int) -> str:
        body = query + "\x1f" + "\x1e".join(documents)
        return f"{KEY_PREFIX}rerank:{self.model_name}:{top_n}:{digest(body)}"

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankResult]:
        key = self._key(query, documents, top_n)
        stored = self.cache.get_many([key])[0]
        if stored is not None:
            try:
                results = [RerankResult(index=int(index), score=float(score))
                           for index, score in json.loads(stored)]
            except (ValueError, TypeError) as exc:
                logger.warning("rerank 缓存内容无法解析，改为调用模型 error=%s", exc)
            else:
                record_lookup(self.cache, self.namespace, "rerank", 1, 0)
                return results
        record_lookup(self.cache, self.namespace, "rerank", 0, 1)
        results = self.inner.rerank(query, documents, top_n)
        encoded = json.dumps([[result.index, result.score] for result in results])
        self.cache.set_many({key: encoded.encode("utf-8")}, jittered(self.ttl_seconds))
        return results
