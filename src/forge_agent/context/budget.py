"""Deterministic, provider-neutral context budgeting."""

from __future__ import annotations

import json
from dataclasses import dataclass

from forge_agent.types import Message


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Estimate tokens without a provider tokenizer.

    UTF-8 byte length is used so the estimate is deterministic on every
    platform and does not severely undercount non-ASCII text.
    """

    context_window: int
    reserved_output_tokens: int
    compaction_ratio: float = 0.75
    bytes_per_token: int = 4
    message_overhead_tokens: int = 4

    def __post_init__(self) -> None:
        if self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if not 0 <= self.reserved_output_tokens < self.context_window:
            raise ValueError("reserved_output_tokens must be within the context window")
        if not 0 < self.compaction_ratio <= 1:
            raise ValueError("compaction_ratio must be in (0, 1]")
        if self.bytes_per_token <= 0:
            raise ValueError("bytes_per_token must be positive")

    @property
    def input_limit(self) -> int:
        return self.context_window - self.reserved_output_tokens

    @property
    def compaction_threshold(self) -> int:
        return int(self.input_limit * self.compaction_ratio)

    def estimate_text(self, text: str | None) -> int:
        if not text:
            return 0
        return _ceil_div(len(text.encode("utf-8")), self.bytes_per_token)

    def estimate_message(self, message: Message) -> int:
        tokens = self.message_overhead_tokens + self.estimate_text(message.role)
        tokens += self.estimate_text(message.content)
        tokens += self.estimate_text(message.tool_call_id)
        if message.tool_calls:
            serialized = json.dumps(
                [call.model_dump(mode="json") for call in message.tool_calls],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            tokens += self.estimate_text(serialized)
        return tokens

    def estimate_messages(self, messages: list[Message]) -> int:
        return sum(self.estimate_message(message) for message in messages)

    def should_compact(self, messages: list[Message], *, fixed_tokens: int = 0) -> bool:
        return fixed_tokens + self.estimate_messages(messages) >= self.compaction_threshold

    def select_for_compaction(
        self,
        messages: list[Message],
        *,
        fixed_tokens: int = 0,
        preserve_recent: int = 8,
        target_ratio: float = 0.60,
    ) -> list[Message]:
        """Select the oldest prefix needed to return below ``target_ratio``.

        At least ``preserve_recent`` messages remain verbatim. The method never
        mutates or deletes the supplied history.
        """

        if preserve_recent < 0:
            raise ValueError("preserve_recent cannot be negative")
        if not 0 < target_ratio < self.compaction_ratio:
            raise ValueError("target_ratio must be below compaction_ratio")
        if not self.should_compact(messages, fixed_tokens=fixed_tokens):
            return []

        eligible_count = max(0, len(messages) - preserve_recent)
        target = int(self.input_limit * target_ratio)
        remaining = fixed_tokens + self.estimate_messages(messages)
        selected: list[Message] = []
        for message in messages[:eligible_count]:
            if remaining <= target:
                break
            selected.append(message)
            remaining -= self.estimate_message(message)
        return selected
