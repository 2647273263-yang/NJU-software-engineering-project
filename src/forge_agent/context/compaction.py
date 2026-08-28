"""Structured, injectable context compaction."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from forge_agent.context.budget import ContextBudget
from forge_agent.storage import CompactionRecord, SQLiteStorage
from forge_agent.types import Message


@dataclass(frozen=True, slots=True)
class CompactionSummary:
    """Provider-neutral structured summary retained across model calls."""

    goal: str = ""
    progress: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CompactionSummary:
        fields = (
            "progress",
            "decisions",
            "files",
            "commands",
            "constraints",
            "open_questions",
            "next_steps",
        )
        return cls(
            goal=str(value.get("goal", "")),
            **{name: [str(item) for item in value.get(name, [])] for name in fields},
        )

    def render(self) -> str:
        sections = [f"Goal: {self.goal}"] if self.goal else []
        labels = (
            ("Progress", self.progress),
            ("Decisions", self.decisions),
            ("Files", self.files),
            ("Commands", self.commands),
            ("Constraints", self.constraints),
            ("Open questions", self.open_questions),
            ("Next steps", self.next_steps),
        )
        for label, items in labels:
            if items:
                sections.append(f"{label}:\n" + "\n".join(f"- {item}" for item in items))
        return "\n\n".join(sections)


class CompactionModel(Protocol):
    """Interface implemented by an application-specific compression model."""

    def summarize(
        self,
        messages: Sequence[Message],
        *,
        previous: CompactionSummary | None = None,
    ) -> CompactionSummary: ...


class CompactionManager:
    """Select and persist summaries while leaving message rows untouched."""

    def __init__(
        self,
        storage: SQLiteStorage,
        budget: ContextBudget,
        model: CompactionModel,
        *,
        preserve_recent: int = 8,
    ) -> None:
        self.storage = storage
        self.budget = budget
        self.model = model
        self.preserve_recent = preserve_recent

    def compact_if_needed(
        self,
        session_id: str,
        *,
        fixed_messages: Sequence[Message] = (),
    ) -> CompactionRecord | None:
        previous_record = self.storage.latest_compaction(session_id)
        previous = (
            CompactionSummary.from_dict(previous_record.summary)
            if previous_record is not None
            else None
        )
        previous_message = (
            Message(role="system", content=previous.render()) if previous is not None else None
        )
        fixed_tokens = self.budget.estimate_messages(
            [*fixed_messages, *([previous_message] if previous_message is not None else [])]
        )
        records = self.storage.list_messages(
            session_id,
            after_id=previous_record.through_message_id if previous_record else None,
        )
        messages = [record.message for record in records]
        selected = self.budget.select_for_compaction(
            messages,
            fixed_tokens=fixed_tokens,
            preserve_recent=self.preserve_recent,
        )
        if not selected:
            return None

        selected_records = records[: len(selected)]
        summary = self.model.summarize(selected, previous=previous)
        retained = records[len(selected_records) :]
        return self.storage.save_compaction(
            session_id,
            through_message_id=selected_records[-1].id,
            retained_from_message_id=retained[0].id if retained else None,
            summary=summary.to_dict(),
        )
