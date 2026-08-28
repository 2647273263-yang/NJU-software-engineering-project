from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from forge_agent.agent import AgentLoop
from forge_agent.config import RunConfig
from forge_agent.model.fake import FakeModel
from forge_agent.storage import SQLiteStorage
from forge_agent.types import Message, ModelResponse, ToolCall, ToolResult


class ReadOnlyFakeTools:
    def schemas(self) -> list[dict[str, Any]]:
        return []

    async def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(ok=True, summary=f"executed {call.name}", content="observation")


def make_config(workspace: Path) -> RunConfig:
    return RunConfig(
        workspace=workspace,
        model="fake",
        api_key=SecretStr("test"),
    )


@pytest.mark.asyncio
async def test_messages_are_persisted_and_can_continue(tmp_path: Path) -> None:
    with SQLiteStorage(tmp_path / "sessions.sqlite3") as storage:
        storage.create_session("resume-test")

        def on_message(message: Message) -> None:
            storage.append_message("resume-test", message)

        first_model = FakeModel(
            [
                ModelResponse(
                    tool_calls=[ToolCall(id="read-1", name="read_file", arguments={})]
                ),
                ModelResponse(text="Initial analysis complete."),
            ]
        )
        first_loop = AgentLoop(
            config=make_config(tmp_path),
            model=first_model,
            tools=ReadOnlyFakeTools(),
            on_message=on_message,
        )
        await first_loop.run("Inspect the project.")

        history = [record.message for record in storage.list_messages("resume-test")]
        assert [message.role for message in history] == [
            "system",
            "user",
            "assistant",
            "tool",
            "assistant",
        ]

        resumed_model = FakeModel([ModelResponse(text="Resumed from persisted context.")])
        resumed_loop = AgentLoop(
            config=make_config(tmp_path),
            model=resumed_model,
            tools=ReadOnlyFakeTools(),
            on_message=on_message,
        )
        await resumed_loop.run("Continue.", history=history)

        persisted = storage.list_messages("resume-test")
        assert len(persisted) == 7
        assert resumed_model.calls[0][-1].content == "Continue."
        assert any(
            message.content == "Initial analysis complete."
            for message in resumed_model.calls[0]
        )
