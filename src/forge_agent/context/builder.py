"""Four-layer context assembly and bounded tool observations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from forge_agent.context.budget import ContextBudget
from forge_agent.context.compaction import CompactionSummary
from forge_agent.types import Message


def truncate_tool_output(output: str, max_chars: int) -> str:
    """Keep both ends of a long tool result within an exact character bound."""

    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if len(output) <= max_chars:
        return output
    marker = f"\n...[truncated {len(output) - max_chars} chars]...\n"
    if len(marker) >= max_chars:
        return marker[:max_chars]
    available = max_chars - len(marker)
    head = (available + 1) // 2
    tail = available - head
    return output[:head] + marker + (output[-tail:] if tail else "")


@dataclass(frozen=True, slots=True)
class ContextLayers:
    """The four context layers in model-visible order."""

    system: tuple[Message, ...]
    project: tuple[Message, ...]
    compaction: tuple[Message, ...]
    recent: tuple[Message, ...]

    @property
    def messages(self) -> list[Message]:
        return [*self.system, *self.project, *self.compaction, *self.recent]


class ContextAssembler:
    def __init__(self, budget: ContextBudget, *, max_tool_output_chars: int = 20_000) -> None:
        if max_tool_output_chars <= 0:
            raise ValueError("max_tool_output_chars must be positive")
        self.budget = budget
        self.max_tool_output_chars = max_tool_output_chars

    def assemble(
        self,
        *,
        system: str | Sequence[Message],
        project: str | None = None,
        compaction: CompactionSummary | None = None,
        recent: Sequence[Message] = (),
    ) -> ContextLayers:
        system_messages = (
            (Message(role="system", content=system),)
            if isinstance(system, str)
            else tuple(system)
        )
        project_messages = (
            (Message(role="system", content=f"[project context]\n{project}"),)
            if project
            else ()
        )
        compaction_messages = (
            (Message(role="system", content=f"[compacted history]\n{compaction.render()}"),)
            if compaction is not None
            else ()
        )
        recent_messages = tuple(self._truncate_tool_message(message) for message in recent)
        return ContextLayers(
            system=system_messages,
            project=project_messages,
            compaction=compaction_messages,
            recent=recent_messages,
        )

    def history_to_compact(
        self,
        layers: ContextLayers,
        *,
        preserve_recent: int = 8,
    ) -> list[Message]:
        fixed = [*layers.system, *layers.project, *layers.compaction]
        return self.budget.select_for_compaction(
            list(layers.recent),
            fixed_tokens=self.budget.estimate_messages(fixed),
            preserve_recent=preserve_recent,
        )

    def _truncate_tool_message(self, message: Message) -> Message:
        if message.role != "tool" or message.content is None:
            return message
        content = truncate_tool_output(message.content, self.max_tool_output_chars)
        if content == message.content:
            return message
        return message.model_copy(update={"content": content})
