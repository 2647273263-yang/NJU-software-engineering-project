from pathlib import Path

import pytest
from pydantic import SecretStr

from forge_agent.agent.loop import AgentLoop
from forge_agent.config import RunConfig
from forge_agent.model.fake import FakeModel
from forge_agent.types import AgentStatus, ModelResponse, ToolCall, ToolResult


class FakeTools:
    def __init__(self, results: list[ToolResult]) -> None:
        self.results = results
        self.calls: list[ToolCall] = []

    def schemas(self) -> list[dict]:
        return []

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        return self.results[len(self.calls) - 1]


@pytest.mark.asyncio
async def test_repeated_failed_verification_retires_hypothesis(tmp_path: Path) -> None:
    failed = ToolResult(
        ok=False,
        summary="failed",
        metadata={
            "verification": {
                "command": "pytest",
                "exit_code": 1,
                "duration_ms": 5,
                "output": "AssertionError",
            }
        },
    )
    model = FakeModel(
        [
            ModelResponse(tool_calls=[ToolCall(id="1", name="verify_changes")]),
            ModelResponse(tool_calls=[ToolCall(id="2", name="verify_changes")]),
            ModelResponse(tool_calls=[ToolCall(id="3", name="verify_changes")]),
            ModelResponse(text="should not be needed"),
        ]
    )
    tools = FakeTools([failed, failed, failed])
    config = RunConfig(workspace=tmp_path, model="fake", api_key=SecretStr("test"))
    events: list[str] = []

    result = await AgentLoop(
        config=config,
        model=model,
        tools=tools,
        on_event=lambda kind, _payload: events.append(kind),
    ).run("fix tests")

    assert result.status is AgentStatus.STOPPED
    assert "same failing verification" in result.summary
    assert events.count("hypothesis_updated") == 2
    assert tools.calls[0].name == "verify_changes"
