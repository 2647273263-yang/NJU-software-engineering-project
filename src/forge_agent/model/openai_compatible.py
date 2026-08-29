"""OpenAI-compatible model adapter with bounded retry behavior."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from typing import Any

import openai
from openai import AsyncOpenAI

from forge_agent.model.base import ContextOverflowError, ModelError
from forge_agent.types import Message, ModelResponse, TokenUsage, ToolCall


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        max_retries: int = 3,
        stream: bool = False,
        on_delta: Callable[[str], None] | None = None,
    ) -> None:
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        self._model = model
        self._max_retries = max_retries
        self._stream = stream
        self._on_delta = on_delta or (lambda _delta: None)

    def set_delta_callback(self, callback: Callable[[str], None]) -> None:
        self._on_delta = callback

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        timeout_s: float,
    ) -> ModelResponse:
        request_messages = [self._serialize_message(message) for message in messages]
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            try:
                request: dict[str, Any] = {
                    "model": self._model,
                    "messages": request_messages,
                }
                if tools:
                    request["tools"] = tools
                    request["tool_choice"] = "auto"
                if self._stream:
                    return await asyncio.wait_for(
                        self._complete_stream(request),
                        timeout=timeout_s,
                    )
                response = await asyncio.wait_for(
                    self._client.chat.completions.create(**request),
                    timeout=timeout_s,
                )
                return self._parse_response(response)
            except Exception as exc:
                error = self._normalize_error(exc)
                if not error.retryable or attempt == attempts - 1:
                    raise error from exc
                delay = min(8.0, 0.5 * (2**attempt)) + random.uniform(0.0, 0.25)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _complete_stream(self, request: dict[str, Any]) -> ModelResponse:
        request["stream"] = True
        request["stream_options"] = {"include_usage": True}
        stream = await self._client.chat.completions.create(**request)
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        request_id: str | None = None
        usage: Any = None
        async for chunk in stream:
            request_id = request_id or getattr(chunk, "id", None)
            usage = getattr(chunk, "usage", None) or usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            finish_reason = choice.finish_reason or finish_reason
            delta = choice.delta
            if delta.content:
                text_parts.append(delta.content)
                self._on_delta(delta.content)
            for raw_call in delta.tool_calls or []:
                entry = calls.setdefault(
                    raw_call.index,
                    {"id": "", "name": "", "arguments": ""},
                )
                if raw_call.id:
                    entry["id"] = raw_call.id
                function = raw_call.function
                if function is not None:
                    if function.name:
                        entry["name"] += function.name
                    if function.arguments:
                        entry["arguments"] += function.arguments
        parsed_calls = [
            self._parse_tool_call(
                value["id"] or f"stream-call-{index}",
                value["name"],
                value["arguments"],
            )
            for index, value in sorted(calls.items())
        ]
        return ModelResponse(
            text="".join(text_parts) or None,
            tool_calls=parsed_calls,
            finish_reason=finish_reason,
            usage=TokenUsage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                estimated=usage is None,
            ),
            request_id=request_id,
        )

    @staticmethod
    def _serialize_message(message: Message) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role}
        if message.content is not None:
            payload["content"] = message.content
        elif message.tool_calls:
            payload["content"] = None
        if message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments
                        or json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        return payload

    @staticmethod
    def _parse_response(response: Any) -> ModelResponse:
        if not response.choices:
            return ModelResponse(request_id=getattr(response, "id", None))
        choice = response.choices[0]
        message = choice.message
        calls: list[ToolCall] = []
        for raw_call in message.tool_calls or []:
            raw_arguments = raw_call.function.arguments or "{}"
            calls.append(
                OpenAICompatibleClient._parse_tool_call(
                    raw_call.id,
                    raw_call.function.name,
                    raw_arguments,
                )
            )
        usage = getattr(response, "usage", None)
        token_usage = TokenUsage(
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            estimated=usage is None,
        )
        return ModelResponse(
            text=message.content,
            tool_calls=calls,
            finish_reason=choice.finish_reason,
            usage=token_usage,
            request_id=getattr(response, "id", None),
        )

    @staticmethod
    def _parse_tool_call(call_id: str, name: str, raw_arguments: str) -> ToolCall:
        try:
            arguments = json.loads(raw_arguments or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
        except (json.JSONDecodeError, ValueError):
            arguments = {}
        return ToolCall(
            id=call_id,
            name=name,
            arguments=arguments,
            raw_arguments=raw_arguments,
        )

    @staticmethod
    def _normalize_error(exc: Exception) -> ModelError:
        message = str(exc)
        lowered = message.lower()
        if "context" in lowered and any(
            marker in lowered for marker in ("length", "window", "maximum", "too long")
        ):
            return ContextOverflowError(message)
        if isinstance(exc, openai.AuthenticationError):
            return ModelError(message, code="authentication", retryable=False)
        if isinstance(exc, openai.PermissionDeniedError):
            return ModelError(message, code="permission_denied", retryable=False)
        if isinstance(exc, openai.RateLimitError):
            return ModelError(message, code="rate_limit", retryable=True)
        if isinstance(exc, (openai.APITimeoutError, asyncio.TimeoutError)):
            return ModelError(message or "model request timed out", code="timeout", retryable=True)
        if isinstance(exc, (openai.APIConnectionError, openai.InternalServerError)):
            return ModelError(message, code="provider_unavailable", retryable=True)
        if isinstance(exc, openai.APIStatusError):
            retryable = exc.status_code >= 500
            return ModelError(message, code=f"http_{exc.status_code}", retryable=retryable)
        return ModelError(message, code="unknown_provider_error", retryable=False)
