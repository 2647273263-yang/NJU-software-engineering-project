import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from forge_agent.agent.loop import AgentLoop, repair_tool_history
from forge_agent.config import RunConfig
from forge_agent.model.fake import FakeModel
from forge_agent.safety.policy import PARALLEL_READ_LIMIT
from forge_agent.types import AgentStatus, Message, ModelResponse, RunMode, TokenUsage, ToolCall, ToolResult


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


class ConcurrentTools:
    def __init__(self, delay_s: float = 0.05) -> None:
        self.delay_s = delay_s
        self.calls: list[ToolCall] = []
        self.active = 0
        self.max_active = 0

    def schemas(self) -> list[dict[str, Any]]:
        return []

    async def execute(self, call: ToolCall) -> ToolResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay_s)
        finally:
            self.active -= 1
        self.calls.append(call)
        return ToolResult(
            ok=True,
            summary=f"read {call.arguments.get('path', call.name)}",
            content=str(call.arguments.get("path", "")),
        )


def _tool_lifecycle(events: list[tuple[str, dict]]) -> list[tuple[str, str]]:
    return [
        (kind, str(payload.get("call_id")))
        for kind, payload in events
        if kind in {"tool_started", "tool_finished"}
    ]


def config(tmp_path: Path) -> RunConfig:
    return RunConfig(
        workspace=tmp_path,
        model="fake",
        api_key=SecretStr("test"),
        max_steps=10,
        max_model_calls=10,
    )


def test_repair_tool_history_inserts_result_before_later_user_messages() -> None:
    history = [
        Message(role="user", content="edit"),
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="call_1", name="write_file", arguments={})],
        ),
        Message(role="user", content="continue"),
        Message(role="tool", tool_call_id="call_1", content="misplaced filler"),
    ]
    repaired = repair_tool_history(history)
    assert [message.role for message in repaired] == [
        "user",
        "assistant",
        "tool",
        "user",
    ]
    assert repaired[2].tool_call_id == "call_1"
    assert repaired[2].content is not None
    assert "interrupted" in repaired[2].content
    assert repair_tool_history(repaired) == repaired


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
async def test_analysis_does_not_verify_historical_edits(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    tools = FakeTools([])
    loop = AgentLoop(
        config=config(tmp_path),
        model=FakeModel([ModelResponse(text="Each sort has a different strength.")]),
        tools=tools,
        on_event=lambda kind, payload: events.append((kind, payload)),
    )
    loop.state.record_changes(["bubble_sort.py"])

    result = await loop.run("分析每一个排序的优点")

    assert result.status is AgentStatus.COMPLETED
    assert result.summary == "Each sort has a different strength."
    assert result.changed_files == []
    assert tools.calls == []
    assert not any(kind == "verification_required" for kind, _ in events)


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


@pytest.mark.asyncio
async def test_read_only_tools_in_one_round_run_in_parallel(tmp_path: Path) -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="r1", name="read_file", arguments={"path": "a.txt"}),
                    ToolCall(id="r2", name="list_files", arguments={"path": "."}),
                    ToolCall(id="r3", name="search_text", arguments={"query": "x"}),
                ]
            ),
            ModelResponse(text="Compared the files"),
        ]
    )
    tools = ConcurrentTools()
    events: list[tuple[str, dict]] = []

    result = await AgentLoop(
        config=config(tmp_path),
        model=model,
        tools=tools,
        on_event=lambda kind, payload: events.append((kind, payload)),
    ).run("inspect three files")

    assert result.status is AgentStatus.COMPLETED
    assert tools.max_active == 3
    assert {call.id for call in tools.calls} == {"r1", "r2", "r3"}
    assert _tool_lifecycle(events) == [
        ("tool_started", "r1"),
        ("tool_started", "r2"),
        ("tool_started", "r3"),
        ("tool_finished", "r1"),
        ("tool_finished", "r2"),
        ("tool_finished", "r3"),
    ]
    tool_messages = [message for message in model.calls[1] if message.role == "tool"]
    assert [message.tool_call_id for message in tool_messages] == ["r1", "r2", "r3"]


@pytest.mark.asyncio
async def test_mixed_read_and_write_round_stays_serial(tmp_path: Path) -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="r1", name="read_file", arguments={"path": "a.txt"}),
                    ToolCall(id="w1", name="replace_in_file", arguments={"path": "a.txt"}),
                ]
            ),
            ModelResponse(text="Edited after reading"),
        ]
    )
    tools = ConcurrentTools()
    events: list[tuple[str, dict]] = []

    result = await AgentLoop(
        config=config(tmp_path),
        model=model,
        tools=tools,
        on_event=lambda kind, payload: events.append((kind, payload)),
    ).run("read then edit")

    assert result.status is AgentStatus.COMPLETED
    assert tools.max_active == 1
    assert _tool_lifecycle(events) == [
        ("tool_started", "r1"),
        ("tool_finished", "r1"),
        ("tool_started", "w1"),
        ("tool_finished", "w1"),
    ]


@pytest.mark.asyncio
async def test_parallel_read_batch_respects_concurrency_limit(tmp_path: Path) -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id=f"r{index}",
                        name="read_file",
                        arguments={"path": f"{index}.txt"},
                    )
                    for index in range(PARALLEL_READ_LIMIT + 1)
                ]
            ),
            ModelResponse(text="Read the batch"),
        ]
    )
    tools = ConcurrentTools()

    result = await AgentLoop(
        config=config(tmp_path),
        model=model,
        tools=tools,
    ).run("read many files")

    assert result.status is AgentStatus.COMPLETED
    assert tools.max_active == PARALLEL_READ_LIMIT
    assert len(tools.calls) == PARALLEL_READ_LIMIT + 1


@pytest.mark.asyncio
async def test_parallel_round_still_stops_on_repeated_action(tmp_path: Path) -> None:
    repeated = ToolCall(id="same", name="read_file", arguments={"path": "a.txt"})
    model = FakeModel(
        [
            ModelResponse(tool_calls=[repeated, repeated, repeated]),
        ]
    )
    tools = ConcurrentTools()

    result = await AgentLoop(
        config=config(tmp_path),
        model=model,
        tools=tools,
    ).run("repeat reads")

    assert result.status is AgentStatus.STOPPED
    assert "three times" in result.summary
    assert tools.max_active == 2
    assert len(tools.calls) == 2


@pytest.mark.asyncio
async def test_plan_mode_delivers_plan_and_stops(tmp_path: Path) -> None:
    approvals: list[str] = []
    modes: list[RunMode] = []
    model = FakeModel(
        [
            ModelResponse(
                text="Goal: analyze the workspace.\nFeasibility: read-only.\nSuggestions: later."
            ),
            ModelResponse(text="should not run"),
        ]
    )
    result = await AgentLoop(
        config=config(tmp_path).model_copy(
            update={"mode": RunMode.PLAN, "auto_approve": True}
        ),
        model=model,
        tools=FakeTools([]),
        on_plan_approval=lambda plan: approvals.append(plan) or True,
        on_mode_change=modes.append,
    ).run("分析工作区")

    assert result.status is AgentStatus.COMPLETED
    assert "analyze the workspace" in result.summary
    assert approvals == []
    assert modes == []
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_agent_plan_first_auto_approves_then_implements(tmp_path: Path) -> None:
    model = FakeModel(
        [
            ModelResponse(
                text="Goal: fix add.\nFeasibility: low risk.\nImplementation: edit app.py."
            ),
            ModelResponse(text="Implemented the approved plan."),
        ]
    )
    modes: list[RunMode] = []
    run_config = config(tmp_path).model_copy(update={"auto_approve": True})
    result = await AgentLoop(
        config=run_config,
        model=model,
        tools=FakeTools([]),
        on_mode_change=modes.append,
    ).run("先给我方案再修")

    assert result.status is AgentStatus.COMPLETED
    assert result.summary == "Implemented the approved plan."
    assert modes == [RunMode.BUILD]
    assert run_config.mode is RunMode.BUILD
    assert any(
        "confirmed the plan" in (message.content or "") for message in model.calls[1]
    )
    system = model.calls[0][0].content or ""
    assert "planning pass" in system
    assert "whether to execute" in system
    assert "Mode: build (planning pass)" in system
    assert "Do not say you are in PLAN mode" in system


@pytest.mark.asyncio
async def test_agent_plan_first_asks_before_executing(tmp_path: Path) -> None:
    decisions: list[str] = []
    model = FakeModel(
        [
            ModelResponse(text="可行性：值得做。\n实现方案：改 app.py。"),
            ModelResponse(text="已按方案执行。"),
        ]
    )
    result = await AgentLoop(
        config=config(tmp_path),
        model=model,
        tools=FakeTools([]),
        on_plan_approval=lambda plan: decisions.append(plan) or True,
    ).run("先给出方案，再执行")

    assert result.status is AgentStatus.COMPLETED
    assert result.summary == "已按方案执行。"
    assert len(decisions) == 1
    assert "可行性" in decisions[0]


@pytest.mark.asyncio
async def test_agent_plan_first_declined_does_not_edit(tmp_path: Path) -> None:
    tools = FakeTools([])
    result = await AgentLoop(
        config=config(tmp_path),
        model=FakeModel(
            [ModelResponse(text="Goal: rewrite.\nFeasibility: risky.")]
        ),
        tools=tools,
        on_plan_approval=lambda _plan: False,
    ).run("先给出方案，再执行")

    assert result.status is AgentStatus.STOPPED
    assert "declined" in result.summary
    assert tools.calls == []


@pytest.mark.asyncio
async def test_agent_plan_first_does_not_run_premature_writes(tmp_path: Path) -> None:
    tools = FakeTools([ToolResult(ok=True, summary="wrote")])
    approvals: list[str] = []
    model = FakeModel(
        [
            ModelResponse(
                text="Goal: update gitignore.\nFeasibility: safe.",
                tool_calls=[
                    ToolCall(
                        id="w",
                        name="write_file",
                        arguments={"path": ".gitignore", "content": "x"},
                    )
                ],
            ),
            ModelResponse(text="Implemented after confirmation."),
        ]
    )
    result = await AgentLoop(
        config=config(tmp_path),
        model=model,
        tools=tools,
        on_plan_approval=lambda plan: approvals.append(plan) or True,
    ).run("先给出方案，再执行")

    assert tools.calls == []
    assert approvals and "gitignore" in approvals[0]
    assert result.summary == "Implemented after confirmation."
