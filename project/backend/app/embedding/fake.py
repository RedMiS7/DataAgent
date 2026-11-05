from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence


class HashEmbedder:
    """Deterministic local embedder for tests and offline demos.

    Uses character bigram hashing so texts sharing phrases stay similar.
    """

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim
        self.model_name = "fake-bigram-v1"

    def _token_vector(self, token: str) -> tuple[int, float]:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % self.dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        return index, sign

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dim
            normalized = " " + text.strip() + " "
            tokens = [normalized[i:i + 2] for i in range(len(normalized) - 1)]
            for token in tokens:
                index, sign = self._token_vector(token)
                vector[index] += sign
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors