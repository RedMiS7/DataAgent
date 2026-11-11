from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from ..cache import Cache, record_lookup
from ..cache.base import KEY_PREFIX, digest, jittered
from .base import EmbeddingProvider


class CachedEmbedder:
    """An embedding provider that sends only the texts it has not seen to the model.

    A model gives the same vector for the same text, so vectors are keyed by model,
    dimension and text. They are stored as float64, so a cached vector equals the one
    the model returned.
    """

    def __init__(self, inner: EmbeddingProvider, cache: Cache, ttl_seconds: int,
                 namespace: str) -> None:
        self.inner = inner
        self.cache = cache
        self.ttl_seconds = ttl_seconds
        self.namespace = namespace
        self.model_name = inner.model_name
        self.dim = inner.dim

    def _key(self, text: str) -> str:
        return f"{KEY_PREFIX}emb:{self.model_name}:{self.dim}:{digest(text)}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        keys = [self._key(text) for text in texts]
        stored = self.cache.get_many(keys)
        vectors: list[list[float] | None] = [
            np.frombuffer(value, dtype="<f8").tolist() if value is not None else None
            for value in stored]
        missing = [index for index, vector in enumerate(vectors) if vector is None]
        record_lookup(self.cache, self.namespace, "emb", len(texts) - len(missing),
                      len(missing))
        if missing:
            fresh = self.inner.embed([texts[index] for index in missing])
            writes: dict[str, bytes] = {}
            for index, vector in zip(missing, fresh, strict=True):
                vectors[index] = vector
                writes[keys[index]] = np.asarray(vector, dtype="<f8").tobytes()
            self.cache.set_many(writes, jittered(self.ttl_seconds))
        return [vector for vector in vectors if vector is not None]
