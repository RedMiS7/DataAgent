"""Record each model and tool call of one question: which agent, how long, how many tokens.

``TraceMiddleware`` hooks into AgentScope's ``on_model_call`` and ``on_acting``,
so every agent that gets ``trace.middlewares()`` is recorded the same way.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any, Literal

from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatResponse, FinishedReason
from agentscope.tool import ToolResponse

DETAIL_LIMIT = 255


@dataclass(slots=True)
class CallRecord:
    agent: str
    kind: Literal["model", "tool"]
    # The model name, or the tool name.
    name: str
    # Milliseconds from the start of the question.
    started_ms: int
    duration_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    # Input tokens the provider read from its prompt cache, part of input_tokens.
    cached_tokens: int = 0
    ok: bool = True
    # The tools a model asked for, or what went wrong.
    detail: str = ""


class CallTrace:
    """The calls of one question. Steps run at once, so records arrive in any order."""

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.records: list[CallRecord] = []

    def middlewares(self) -> list[MiddlewareBase]:
        return [TraceMiddleware(self)]

    def add(self, agent: str, kind: Literal["model", "tool"], name: str, started: float,
            *, input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0,
            ok: bool = True, detail: str = "") -> None:
        now = time.perf_counter()
        self.records.append(CallRecord(
            agent=agent, kind=kind, name=name,
            started_ms=int((started - self.started) * 1000),
            duration_ms=int((now - started) * 1000),
            input_tokens=input_tokens, output_tokens=output_tokens,
            cached_tokens=cached_tokens, ok=ok,
            detail=detail[:DETAIL_LIMIT],
        ))

    def ordered(self) -> list[CallRecord]:
        return sorted(self.records, key=lambda record: record.started_ms)

    def _models(self) -> list[CallRecord]:
        return [record for record in self.records if record.kind == "model"]

    @property
    def input_tokens(self) -> int:
        return sum(record.input_tokens for record in self._models())

    @property
    def output_tokens(self) -> int:
        return sum(record.output_tokens for record in self._models())

    @property
    def cached_tokens(self) -> int:
        return sum(record.cached_tokens for record in self._models())

    def usage(self) -> dict[str, int]:
        models = self._models()
        return {
            "model_calls": len(models),
            "tool_calls": len(self.records) - len(models),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_tokens,
            # Steps run at once, so this can exceed the time the question took.
            "model_ms": sum(record.duration_ms for record in models),
        }


def _failure(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".rstrip(": ")


class TraceMiddleware(MiddlewareBase):
    def __init__(self, trace: CallTrace) -> None:
        self.trace = trace

    def _record_response(self, agent: Any, name: str, started: float,
                         response: ChatResponse) -> None:
        usage = response.usage
        # The model wrapper turns a cancel (a timeout) into an empty, interrupted response.
        if response.finished_reason == FinishedReason.INTERRUPTED:
            self.trace.add(agent.name, "model", name, started, ok=False, detail="interrupted")
            return
        tools = [block.name for block in response.content if getattr(block, "type", "") == "tool_call"]
        self.trace.add(agent.name, "model", name, started,
                       input_tokens=usage.input_tokens if usage else 0,
                       output_tokens=usage.output_tokens if usage else 0,
                       cached_tokens=usage.cache_input_tokens if usage else 0,
                       detail=",".join(tools))

    async def on_model_call(self, agent: Any, input_kwargs: dict,
                            next_handler: Callable[..., Any]) -> Any:
        name = getattr(input_kwargs.get("current_model"), "model", "")
        started = time.perf_counter()
        try:
            response = await next_handler()
        except BaseException as exc:
            self.trace.add(agent.name, "model", name, started, ok=False, detail=_failure(exc))
            raise
        if isinstance(response, ChatResponse):
            self._record_response(agent, name, started, response)
            return response
        return self._stream(agent, name, started, response)

    async def _stream(self, agent: Any, name: str, started: float,
                      chunks: AsyncGenerator[ChatResponse, None]
                      ) -> AsyncGenerator[ChatResponse, None]:
        last: ChatResponse | None = None
        async for chunk in chunks:
            last = chunk
            yield chunk
        if last is not None:
            self._record_response(agent, name, started, last)

    async def on_acting(self, agent: Any, input_kwargs: dict,
                        next_handler: Callable[..., AsyncGenerator]) -> AsyncGenerator:
        name = input_kwargs["tool_call"].name
        started = time.perf_counter()
        state = "success"
        try:
            async for item in next_handler():
                if isinstance(item, ToolResponse):
                    state = str(item.state)
                yield item
        except BaseException as exc:
            self.trace.add(agent.name, "tool", name, started, ok=False, detail=_failure(exc))
            raise
        ok = state == "success"
        self.trace.add(agent.name, "tool", name, started, ok=ok, detail="" if ok else state)


__all__ = ["CallRecord", "CallTrace", "TraceMiddleware"]
