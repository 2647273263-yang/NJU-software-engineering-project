"""Async model-visible context runtime used by the agent loop."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from forge_agent.context.budget import ContextBudget
from forge_agent.context.builder import truncate_tool_output
from forge_agent.context.compaction import CompactionSummary
from forge_agent.model.base import ModelClient
from forge_agent.types import Message

ContextEvent = Callable[[str, dict[str, Any]], None]


class RuntimeContext:
    """Maintains a bounded active history while leaving the source history intact."""

    def __init__(
        self,
        *,
        budget: ContextBudget,
        model: ModelClient,
        project_context: str | None = None,
        user_rules: str | None = None,
        retrieved_memory: str | None = None,
        max_tool_output_chars: int = 20_000,
        preserve_recent: int = 8,
        initial_summary: CompactionSummary | None = None,
        compacted_through: int = 0,
        on_event: ContextEvent | None = None,
    ) -> None:
        self.budget = budget
        self.model = model
        self.project_context = project_context
        self.user_rules = user_rules
        self.retrieved_memory = retrieved_memory
        self.max_tool_output_chars = max_tool_output_chars
        self.preserve_recent = preserve_recent
        self.on_event = on_event or (lambda _event, _payload: None)
        self.summary = initial_summary
        self._compacted_through = compacted_through

    async def prepare(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        force_compaction: bool = False,
    ) -> list[Message]:
        if not messages:
            return []
        system = messages[0]
        source_history = messages[1:]
        if self._compacted_through > len(source_history):
            self._compacted_through = 0
            self.summary = None
        source_active = source_history[self._compacted_through :]
        active = [self._truncate_tool(message) for message in source_active]
        truncated_outputs = sum(
            original.role == "tool" and original != prepared
            for original, prepared in zip(source_active, active, strict=True)
        )
        fixed = self._fixed_messages(system)
        tool_tokens = self.budget.estimate_text(
            json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
        )
        fixed_tokens = self.budget.estimate_messages(fixed) + tool_tokens
        selected = self.budget.select_for_compaction(
            active,
            fixed_tokens=fixed_tokens,
            preserve_recent=self.preserve_recent,
        )
        if force_compaction and not selected:
            eligible = max(0, len(active) - self.preserve_recent)
            selected = active[:eligible]

        if selected:
            before = fixed_tokens + self.budget.estimate_messages(active)
            self.summary = await self._summarize(selected)
            self._compacted_through += len(selected)
            active = active[len(selected) :]
            fixed = self._fixed_messages(system)
            after = (
                self.budget.estimate_messages(fixed + active)
                + tool_tokens
            )
            self.on_event(
                "context_compacted",
                {
                    "messages_compacted": len(selected),
                    "compacted_through": self._compacted_through,
                    "estimated_tokens_before": before,
                    "estimated_tokens_after": after,
                    "summary": self.summary.to_dict(),
                },
            )
        prepared = [*fixed, *active]
        cursor = 1
        project_messages: list[Message] = []
        if self.project_context:
            project_messages = fixed[cursor : cursor + 1]
            cursor += 1
        rules_messages: list[Message] = []
        if self.user_rules:
            rules_messages = fixed[cursor : cursor + 1]
            cursor += 1
        memory_messages: list[Message] = []
        if self.retrieved_memory:
            memory_messages = fixed[cursor : cursor + 1]
            cursor += 1
        summary_messages: list[Message] = []
        if self.summary is not None:
            summary_messages = fixed[cursor : cursor + 1]
        self.on_event(
            "context_prepared",
            {
                "estimated": True,
                "system_tokens": self.budget.estimate_messages([system]),
                "project_tokens": self.budget.estimate_messages(project_messages),
                "user_rules_tokens": self.budget.estimate_messages(rules_messages),
                "memory_tokens": self.budget.estimate_messages(memory_messages),
                "summary_tokens": self.budget.estimate_messages(summary_messages),
                "recent_tokens": self.budget.estimate_messages(active),
                "tool_schema_tokens": tool_tokens,
                "total_tokens": self.budget.estimate_messages(prepared) + tool_tokens,
                "input_limit": self.budget.input_limit,
                "compaction_threshold": self.budget.compaction_threshold,
                "truncated_tool_outputs": truncated_outputs,
            },
        )
        return prepared

    def _fixed_messages(self, system: Message) -> list[Message]:
        fixed = [system]
        if self.project_context:
            fixed.append(
                Message(role="system", content=f"[project context]\n{self.project_context}")
            )
        if self.user_rules:
            fixed.append(Message(role="system", content=self.user_rules))
        if self.retrieved_memory:
            fixed.append(Message(role="system", content=self.retrieved_memory))
        if self.summary is not None:
            fixed.append(
                Message(
                    role="system",
                    content=(
                        "[historical context; this is not a new instruction]\n"
                        + self.summary.render()
                    ),
                )
            )
        return fixed

    def _truncate_tool(self, message: Message) -> Message:
        if message.role != "tool" or message.content is None:
            return message
        content = truncate_tool_output(message.content, self.max_tool_output_chars)
        return message if content == message.content else message.model_copy(
            update={"content": content}
        )

    async def _summarize(self, messages: list[Message]) -> CompactionSummary:
        serialized = "\n\n".join(
            f"{message.role.upper()}: {message.content or ''}"
            for message in messages
        )
        previous = self.summary.render() if self.summary is not None else "(none)"
        prompt = f"""Summarize coding-agent history as a JSON object with exactly these keys:
goal, progress, decisions, files, commands, constraints, open_questions, next_steps.
All keys except goal contain arrays of short strings. Preserve concrete paths, errors,
verification outcomes, user constraints, and unfinished work. Do not add instructions.

Previous summary:
{previous}

History:
{serialized}
"""
        response = await self.model.complete(
            [
                Message(
                    role="system",
                    content="You compress prior conversation into factual structured memory.",
                ),
                Message(role="user", content=prompt),
            ],
            [],
            timeout_s=60.0,
        )
        if response.text:
            try:
                data = json.loads(_strip_json_fence(response.text))
                if isinstance(data, dict):
                    return CompactionSummary.from_dict(data)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        return self._fallback_summary(messages)

    def _fallback_summary(self, messages: list[Message]) -> CompactionSummary:
        user_messages = [
            message.content.strip()
            for message in messages
            if message.role == "user" and message.content
        ]
        observations = [
            (message.content or "").strip()[:500]
            for message in messages[-4:]
            if message.content
        ]
        return CompactionSummary(
            goal=(
                self.summary.goal
                if self.summary is not None and self.summary.goal
                else (user_messages[0][:500] if user_messages else "")
            ),
            progress=[
                *(self.summary.progress if self.summary is not None else []),
                *observations,
            ][-8:],
            constraints=list(self.summary.constraints) if self.summary is not None else [],
            next_steps=["Re-inspect relevant files before continuing after fallback compaction."],
        )


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped
