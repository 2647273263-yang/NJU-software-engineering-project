"""Deterministic model used by tests and offline demonstrations."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
from typing import Any

from forge_agent.types import Message, ModelResponse

FakeStep = ModelResponse | Exception | Callable[[list[Message]], ModelResponse]


class FakeModel:
    def __init__(self, steps: Iterable[FakeStep]) -> None:
        self._steps = list(steps)
        self.calls: list[list[Message]] = []
        self.tool_schemas: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        timeout_s: float,
    ) -> ModelResponse:
        del timeout_s
        self.calls.append(deepcopy(messages))
        self.tool_schemas.append(deepcopy(tools))
        index = len(self.calls) - 1
        if index >= len(self._steps):
            raise RuntimeError("FakeModel has no scripted response for this call")
        step = self._steps[index]
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(messages)
        return step.model_copy(deep=True)
