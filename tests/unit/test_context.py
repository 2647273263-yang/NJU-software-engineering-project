from __future__ import annotations

from collections.abc import Sequence

import pytest

from forge_agent.context import (
    CompactionManager,
    CompactionSummary,
    ContextAssembler,
    ContextBudget,
    RuntimeContext,
    truncate_tool_output,
)
from forge_agent.model.fake import FakeModel
from forge_agent.storage import SQLiteStorage
from forge_agent.types import Message, ModelResponse


class FakeCompactionModel:
    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def summarize(
        self,
        messages: Sequence[Message],
        *,
        previous: CompactionSummary | None = None,
    ) -> CompactionSummary:
        self.calls.append(list(messages))
        prior_progress = previous.progress if previous else []
        return CompactionSummary(
            goal="complete the task",
            progress=[*prior_progress, f"summarized {len(messages)} messages"],
            next_steps=["continue"],
        )


def test_token_estimate_is_deterministic_and_utf8_aware() -> None:
    budget = ContextBudget(context_window=1_000, reserved_output_tokens=100)

    assert budget.estimate_text("abcd") == 1
    assert budget.estimate_text("你好") == 2
    message = Message(role="user", content="same")
    assert budget.estimate_message(message) == budget.estimate_message(message)
    assert budget.compaction_threshold == 675


def test_assembles_four_layers_and_truncates_tool_output() -> None:
    budget = ContextBudget(context_window=1_000, reserved_output_tokens=100)
    assembler = ContextAssembler(budget, max_tool_output_chars=80)
    original = "BEGIN-" + ("x" * 200) + "-END"

    layers = assembler.assemble(
        system="system rules",
        project="project facts",
        compaction=CompactionSummary(goal="goal", decisions=["keep SQLite"]),
        recent=[
            Message(role="user", content="run it"),
            Message(role="tool", content=original, tool_call_id="call-1"),
        ],
    )

    assert [message.role for message in layers.messages] == [
        "system",
        "system",
        "system",
        "user",
        "tool",
    ]
    output = layers.recent[-1].content
    assert output is not None
    assert len(output) == 80
    assert output.startswith("BEGIN-")
    assert output.endswith("-END")
    assert "[truncated" in output
    assert truncate_tool_output("short", 10) == "short"


def test_selects_oldest_history_only_after_threshold() -> None:
    budget = ContextBudget(
        context_window=240,
        reserved_output_tokens=40,
        compaction_ratio=0.75,
    )
    history = [Message(role="user", content=str(index) * 40) for index in range(12)]

    selected = budget.select_for_compaction(history, preserve_recent=2)

    assert selected
    assert selected == history[: len(selected)]
    assert len(selected) <= 10
    assert budget.select_for_compaction(history[:2], preserve_recent=2) == []


def test_compaction_is_structured_and_does_not_delete_history(tmp_path) -> None:
    budget = ContextBudget(context_window=240, reserved_output_tokens=40)
    model = FakeCompactionModel()
    with SQLiteStorage(tmp_path / "state.db") as storage:
        storage.create_session("session-1")
        for index in range(12):
            storage.append_message(
                "session-1",
                Message(role="user", content=f"{index:02d}" + ("x" * 40)),
            )

        manager = CompactionManager(storage, budget, model, preserve_recent=2)
        record = manager.compact_if_needed("session-1")

        assert record is not None
        assert record.summary["goal"] == "complete the task"
        assert record.retained_from_message_id is not None
        assert len(model.calls) == 1
        assert len(storage.list_messages("session-1")) == 12


@pytest.mark.asyncio
async def test_runtime_context_compacts_and_keeps_recent_messages() -> None:
    model = FakeModel(
        [
            ModelResponse(
                text=(
                    '{"goal":"fix bug","progress":["read app.py"],'
                    '"decisions":[],"files":["app.py"],"commands":[],'
                    '"constraints":[],"open_questions":[],"next_steps":["edit app.py"]}'
                )
            )
        ]
    )
    events: list[tuple[str, dict[str, object]]] = []
    runtime = RuntimeContext(
        budget=ContextBudget(context_window=240, reserved_output_tokens=40),
        model=model,
        preserve_recent=2,
        on_event=lambda kind, payload: events.append((kind, payload)),
    )
    messages = [
        Message(role="system", content="rules"),
        *[
            Message(role="user", content=f"message-{index}-" + ("x" * 50))
            for index in range(12)
        ],
    ]

    prepared = await runtime.prepare(messages, [])

    assert runtime.summary is not None
    assert runtime.summary.goal == "fix bug"
    assert prepared[0].role == "system"
    assert "historical context" in (prepared[1].content or "")
    assert prepared[-2:] == messages[-2:]
    assert events[0][0] == "context_compacted"


@pytest.mark.asyncio
async def test_runtime_context_injects_user_rules_before_recent_messages() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    runtime = RuntimeContext(
        budget=ContextBudget(context_window=8_000, reserved_output_tokens=1_000),
        model=FakeModel([]),
        user_rules="[user rules]\nPrefer pytest.",
        on_event=lambda kind, payload: events.append((kind, payload)),
    )
    messages = [
        Message(role="system", content="core rules"),
        Message(role="user", content="run tests"),
    ]

    prepared = await runtime.prepare(messages, [])

    assert prepared[0].content == "core rules"
    assert prepared[1].content is not None
    assert prepared[1].content.startswith("[user rules]")
    assert prepared[2].content == "run tests"
    prepared_event = next(payload for kind, payload in events if kind == "context_prepared")
    assert int(prepared_event["user_rules_tokens"]) > 0
