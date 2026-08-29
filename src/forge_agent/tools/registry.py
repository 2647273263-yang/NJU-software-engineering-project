"""Small, framework-free tool specification and registry."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from forge_agent.types import ToolCall, ToolResult

ToolHandler = Callable[[Any], ToolResult | Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    arguments: type[BaseModel]
    handler: ToolHandler

    def json_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.arguments.model_json_schema(),
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.json_schema() for spec in self._tools.values()]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        try:
            spec = self.get(name)
            validated = spec.arguments.model_validate(arguments)
            handler = spec.handler
            if inspect.iscoroutinefunction(getattr(handler, "__func__", handler)):
                result = await handler(validated)
            else:
                result = await asyncio.to_thread(handler, validated)
        except (ValidationError, KeyError) as exc:
            error_code = (
                "invalid_arguments" if isinstance(exc, ValidationError) else "unknown_tool"
            )
            result = ToolResult(
                ok=False,
                summary=str(exc),
                error_code=error_code,
            )
        except Exception as exc:
            result = ToolResult(ok=False, summary=str(exc), error_code=type(exc).__name__)
        result.duration_ms = int((time.monotonic() - started) * 1_000)
        return result

    async def execute(self, call: ToolCall) -> ToolResult:
        return await self.call(call.name, call.arguments)

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._tools.values())
