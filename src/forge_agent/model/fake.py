"""Deterministic model used by tests and offline demonstrations."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
from typing import Any

from forge_agent.types import Message, ModelResponse

FakeStep = ModelResponse | Exception | Callable[[list[Message]], ModelResponse]


class FakeModel:
    def __init__(
        self,
        steps: Iterable[FakeStep],
        *,
        judge_steps: Iterable[FakeStep] | None = None,
    ) -> None:
        self._steps = list(steps)
        self._judge_steps = list(judge_steps or [])
        self.calls: list[list[Message]] = []
        self.judge_calls: list[list[Message]] = []
        self.tool_schemas: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        timeout_s: float,
    ) -> ModelResponse:
        del timeout_s
        if _is_judge_request(messages, tools):
            self.judge_calls.append(deepcopy(messages))
            if self._judge_steps:
                if len(self.judge_calls) > len(self._judge_steps):
                    raise RuntimeError("FakeModel has no scripted judge response for this call")
                step = self._judge_steps[len(self.judge_calls) - 1]
                if isinstance(step, Exception):
                    raise step
                if callable(step):
                    return step(messages)
                return step.model_copy(deep=True)
            return ModelResponse(
                text='{"accepted": true, "reason": "FakeModel default accept.", "missing": []}'
            )
        if len(self.calls) >= len(self._steps):
            raise RuntimeError("FakeModel has no scripted response for this call")
        self.calls.append(deepcopy(messages))
        self.tool_schemas.append(deepcopy(tools))
        step = self._steps[len(self.calls) - 1]
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(messages)
        return step.model_copy(deep=True)


def _is_judge_request(messages: list[Message], tools: list[dict[str, Any]]) -> bool:
    if tools or len(messages) != 2:
        return False
    if messages[0].role != "system" or messages[1].role != "user":
        return False
    system = (messages[0].content or "").lower()
    user = (messages[1].content or "").lower()
    return (
        "acceptance inspector" in system
        or "llm judge" in system
        or user.startswith("original user task:")
        or "\noriginal user task:\n" in user
    )
