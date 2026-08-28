from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from forge_agent.agent.loop import AgentLoop
from forge_agent.config import RunConfig
from forge_agent.model.fake import FakeModel
from forge_agent.types import AgentStatus, ModelResponse, TokenUsage, ToolCall, ToolResult


class FakeTools:
    def __init__(self, results: list[ToolResult]) -> None:
        self.results = results
        self.calls: list[ToolCall] = []

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "fake",
                    "description": "fake",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        return self.results[len(self.calls) - 1]


def config(tmp_path: Path) -> RunConfig:
    return RunConfig(
        workspace=tmp_path,
        model="fake",
        api_key=SecretStr("test"),
        max_steps=10,
        max_model_calls=10,
    )


@pytest.mark.asyncio
async def test_executes_tool_then_completes(tmp_path: Path) -> None:
    model = FakeModel(
        [
            ModelResponse(tool_calls=[ToolCall(id="1", name="fake")]),
            ModelResponse(text="Done"),
        ]
    )
    tools = FakeTools([ToolResult(ok=True, summary="read")])
    events: list[tuple[str, dict]] = []

    result = await AgentLoop(
        config=config(tmp_path),
        model=model,
        tools=tools,
        on_event=lambda kind, payload: events.append((kind, payload)),
    ).run("inspect")

    assert result.status is AgentStatus.COMPLETED
    assert result.summary == "Done"
    assert [call.name for call in tools.calls] == ["fake"]
    assert model.calls[1][-1].role == "tool"
    answers = [
        payload.get("text")
        for kind, payload in events
        if kind == "model_response" and payload.get("tool_calls") == 0
    ]
    assert answers[-1] == "Done"


@pytest.mark.asyncio
async def test_requires_verification_after_edit(tmp_path: Path) -> None:
    model = FakeModel(
        [
            ModelResponse(tool_calls=[ToolCall(id="1", name="fake")]),
            ModelResponse(text="Done without tests"),
            ModelResponse(tool_calls=[ToolCall(id="2", name="verify_changes")]),
            ModelResponse(text="Done with tests"),
        ]
    )
    tools = FakeTools(
        [
            ToolResult(
                ok=True,
                summary="edited",
                metadata={"changed_files": ["app.py"]},
            ),
            ToolResult(
                ok=True,
                summary="verified",
                metadata={
                    "verification": {
                        "command": "pytest",
                        "exit_code": 0,
                        "duration_ms": 10,
                        "output": "1 passed",
                    }
                },
            ),
        ]
    )

    result = await AgentLoop(config=config(tmp_path), model=model, tools=tools).run("fix")

    assert result.status is AgentStatus.COMPLETED
    assert result.verification is not None
    assert result.verification.passed
    assert result.changed_files == ["app.py"]


@pytest.mark.asyncio
async def test_stops_repeated_action(tmp_path: Path) -> None:
    repeated = ModelResponse(tool_calls=[ToolCall(id="x", name="fake", arguments={"a": 1})])
    model = FakeModel([repeated, repeated, repeated])
    tools = FakeTools(
        [
            ToolResult(ok=False, summary="failed"),
            ToolResult(ok=False, summary="failed"),
        ]
    )

    result = await AgentLoop(config=config(tmp_path), model=model, tools=tools).run("loop")

    assert result.status is AgentStatus.STOPPED
    assert "three times" in result.summary


@pytest.mark.asyncio
async def test_stops_at_token_budget(tmp_path: Path) -> None:
    run_config = config(tmp_path).model_copy(update={"max_total_tokens": 1_000})
    model = FakeModel(
        [
            ModelResponse(
                text="This response exceeds the configured budget.",
                usage=TokenUsage(input_tokens=900, output_tokens=100),
            )
        ]
    )

    result = await AgentLoop(
        config=run_config,
        model=model,
        tools=FakeTools([]),
    ).run("budget")

    assert result.status is AgentStatus.STOPPED
    assert result.total_tokens == 1_000
    assert "token budget" in result.summary


@pytest.mark.asyncio
async def test_stops_at_cost_budget(tmp_path: Path) -> None:
    run_config = config(tmp_path).model_copy(
        update={
            "max_cost_usd": 0.001,
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 2.0,
        }
    )
    model = FakeModel(
        [
            ModelResponse(
                text="This response reaches the configured cost budget.",
                usage=TokenUsage(input_tokens=1_000, output_tokens=100),
            )
        ]
    )

    result = await AgentLoop(
        config=run_config,
        model=model,
        tools=FakeTools([]),
    ).run("budget")

    assert result.status is AgentStatus.STOPPED
    assert result.total_cost_usd == pytest.approx(0.0012)
    assert "cost budget" in result.summary


@pytest.mark.asyncio
async def test_stops_after_two_empty_responses(tmp_path: Path) -> None:
    model = FakeModel([ModelResponse(), ModelResponse()])

    result = await AgentLoop(
        config=config(tmp_path),
        model=model,
        tools=FakeTools([]),
    ).run("empty")

    assert result.status is AgentStatus.FAILED
    assert "two consecutive empty" in result.summary
