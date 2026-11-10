"""Alibaba Cloud Bailian (DashScope) text rerank, through its native HTTP API."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import httpx

from ..service_calls import retry_async
from .base import RerankError, RerankResult


class DashScopeReranker:
    def __init__(self, endpoint: str, model: str, api_key: str, timeout: float) -> None:
        self.endpoint = endpoint
        self.model_name = model
        self._api_key = api_key
        self._timeout = timeout

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankResult]:
        if not documents:
            return []
        body = {
            "model": self.model_name,
            "input": {"query": query, "documents": list(documents)},
            "parameters": {"top_n": min(top_n, len(documents))},
        }
        try:
            data = asyncio.run(self._request(body))
        except (httpx.HTTPError, TimeoutError) as exc:
            raise RerankError(f"重排模型请求失败：{type(exc).__name__}: {exc}") from exc
        try:
            results = [RerankResult(index=int(item["index"]), score=float(item["relevance_score"]))
                       for item in data["output"]["results"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise RerankError(f"重排模型返回的格式不对：{exc}") from exc
        valid = [result for result in results if 0 <= result.index < len(documents)]
        return sorted(valid, key=lambda result: result.score, reverse=True)[:top_n]

    async def _request(self, body: dict[str, Any]) -> Any:
        async with httpx.AsyncClient() as client:
            async def post(seconds: float) -> Any:
                response = await client.post(
                    self.endpoint, json=body, timeout=seconds,
                    headers={"Authorization": f"Bearer {self._api_key}"})
                return self._decode(response)

            return await retry_async("rerank", self._timeout, post)

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        try:
            data = response.json()
        except ValueError as exc:
            raise RerankError(f"重排模型返回的不是 JSON：HTTP {response.status_code}") from exc
        if response.status_code != 200:
            if not isinstance(data, dict):
                data = {}
            raise RerankError(f"重排模型返回 HTTP {response.status_code}："
                              f"{data.get('code', '')} {data.get('message', '')}".rstrip())
        return data
