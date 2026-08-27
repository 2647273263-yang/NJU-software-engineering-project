"""Model abstraction kept independent from any provider SDK."""

from __future__ import annotations

from typing import Any, Protocol

from forge_agent.types import Message, ModelResponse


class ModelClient(Protocol):
    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        timeout_s: float,
    ) -> ModelResponse: ...


class ModelError(RuntimeError):
    """Normalized provider failure."""

    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ContextOverflowError(ModelError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="context_overflow", retryable=False)
