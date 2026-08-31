from pathlib import Path

import pytest
from pydantic import SecretStr

from forge_agent.application import SessionService
from forge_agent.config import RunConfig
from forge_agent.model.fake import FakeModel
from forge_agent.safety import SPAWN_EXPLORE, PolicyEngine, RiskLevel
from forge_agent.storage import SQLiteStorage
from forge_agent.types import AgentStatus, ModelResponse, RunMode, ToolCall


def _config(workspace: Path) -> RunConfig:
    return RunConfig(
        workspace=workspace,
        model="fake",
        api_key=SecretStr("test"),
        auto_approve=True,
        max_steps=12,
        max_model_calls=12,
    )


def test_policy_allows_spawn_explore_without_approval() -> None:
    policy = PolicyEngine()
    decision = policy.evaluate(SPAWN_EXPLORE, {"task": "find sort"})
    plan = PolicyEngine(mode=RunMode.PLAN).evaluate(SPAWN_EXPLORE, {"task": "find sort"})
    assert decision.risk is RiskLevel.LOW
    assert decision.allowed
    assert not decision.requires_approval
    assert plan.allowed


@pytest.mark.asyncio
async def test_spawn_explore_returns_conclusion_without_file_body(
    tmp_path: Path,
) -> None:
    secret_body = "UNIQUE_SORT_BODY_xyz"
    (tmp_path / "heap_sort.py").write_text(
        f"def heap_sort():\n    {secret_body}\n",
        encoding="utf-8",
    )
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="1",
                        name=SPAWN_EXPLORE,
                        arguments={"task": "Where is heap sort implemented?"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="s1", name="read_file", arguments={"path": "heap_sort.py"})
                ]
            ),
            ModelResponse(text="Heap sort lives in heap_sort.py."),
            ModelResponse(text="I will edit heap_sort.py next."),
        ]
    )
    service = SessionService(tmp_path / "forge.db", model_factory=lambda _config: model)
    running = service.start_new(_config(tmp_path), "Improve heap sort.")
    result = await running.task
    session_id = running.id

    with SQLiteStorage(tmp_path / "forge.db") as storage:
        texts = [record.message.content or "" for record in storage.list_messages(session_id)]
        events = list(storage.list_events(session_id))

    parent_tool = [text for text in texts if "Heap sort lives in heap_sort.py." in text]
    assert result.status is AgentStatus.COMPLETED
    assert parent_tool
    assert all(secret_body not in text for text in texts)
    assert any(
        event.kind == "tool_finished"
        and event.payload.get("name") == SPAWN_EXPLORE
        and event.payload.get("ok")
        for event in events
    )
    assert any(event.payload.get("source") == "subagent" for event in events)
    assert not any(event.kind == "run_finished" and event.payload.get("source") for event in events)


@pytest.mark.asyncio
async def test_spawn_explore_strips_write_tools(tmp_path: Path) -> None:
    (tmp_path / "readme.txt").write_text("ok\n", encoding="utf-8")
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="1",
                        name=SPAWN_EXPLORE,
                        arguments={
                            "task": "survey",
                            "tools": ["write_file", "read_file"],
                        },
                    )
                ]
            ),
            ModelResponse(text="Read-only survey done."),
            ModelResponse(text="Survey received."),
        ]
    )
    service = SessionService(tmp_path / "forge.db", model_factory=lambda _config: model)
    running = service.start_new(_config(tmp_path), "Look around.")
    result = await running.task
    assert result.status is AgentStatus.COMPLETED
    assert not (tmp_path / "stolen.txt").exists()
    with SQLiteStorage(tmp_path / "forge.db") as storage:
        finished = [
            event.payload
            for event in storage.list_events(running.id)
            if event.kind == "tool_finished" and event.payload.get("name") == SPAWN_EXPLORE
        ]
    assert finished
    assert "write_file" in (finished[0].get("metadata") or {}).get("stripped_tools", [])


@pytest.mark.asyncio
async def test_spawn_explore_stops_at_step_limit(tmp_path: Path) -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="1",
                        name=SPAWN_EXPLORE,
                        arguments={"task": "keep looking", "max_steps": 1},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="s1", name="list_files", arguments={"path": "."})
                ]
            ),
            ModelResponse(text="Parent continued after the explorer stopped."),
        ]
    )
    service = SessionService(tmp_path / "forge.db", model_factory=lambda _config: model)
    running = service.start_new(_config(tmp_path), "Explore then stop.")
    result = await running.task
    assert result.status is AgentStatus.COMPLETED
    with SQLiteStorage(tmp_path / "forge.db") as storage:
        finished = [
            event.payload
            for event in storage.list_events(running.id)
            if event.kind == "tool_finished"
            and event.payload.get("name") == SPAWN_EXPLORE
            and event.payload.get("source") != "subagent"
        ]
    assert finished
    assert finished[0].get("ok") is False
    assert finished[0].get("metadata", {}).get("status") == AgentStatus.STOPPED.value


@pytest.mark.asyncio
async def test_explore_child_cannot_spawn_again(tmp_path: Path) -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="1", name=SPAWN_EXPLORE, arguments={"task": "nest"})
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="s1", name=SPAWN_EXPLORE, arguments={"task": "inner"})
                ]
            ),
            ModelResponse(text="Nested spawn was not available."),
            ModelResponse(text="Parent saw the explore result."),
        ]
    )
    service = SessionService(tmp_path / "forge.db", model_factory=lambda _config: model)
    running = service.start_new(_config(tmp_path), "Explore once.")
    result = await running.task
    assert result.status is AgentStatus.COMPLETED
    with SQLiteStorage(tmp_path / "forge.db") as storage:
        child_spawn = [
            event.payload
            for event in storage.list_events(running.id)
            if event.kind == "tool_finished"
            and event.payload.get("name") == SPAWN_EXPLORE
            and event.payload.get("source") == "subagent"
        ]
    assert child_spawn
    assert child_spawn[0].get("ok") is False
