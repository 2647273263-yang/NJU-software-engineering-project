import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from forge_agent.application import ApprovalBroker, EventBus, SessionService
from forge_agent.config import RunConfig
from forge_agent.model.fake import FakeModel
from forge_agent.safety import PolicyDecision, RiskLevel
from forge_agent.storage import SQLiteStorage
from forge_agent.types import (
    AgentStatus,
    Message,
    ModelResponse,
    ToolCall,
)


def config(workspace: Path, *, auto_approve: bool = True) -> RunConfig:
    return RunConfig(
        workspace=workspace,
        model="fake",
        api_key=SecretStr("test"),
        auto_approve=auto_approve,
    )


def test_event_bus_fans_out_events() -> None:
    bus = EventBus()
    first = bus.subscribe()
    second = bus.subscribe()

    bus.publish("session", "tool_started", {"name": "read_file"})

    assert first.get_nowait().kind == "tool_started"
    assert second.get_nowait().session_id == "session"


@pytest.mark.asyncio
async def test_approval_broker_resolves_future() -> None:
    bus = EventBus()
    broker = ApprovalBroker(bus)
    call = ToolCall(id="call", name="write_file", arguments={"path": "app.py"})
    decision = PolicyDecision(
        allowed=True,
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        reason="workspace modification",
    )

    pending = asyncio.create_task(broker.request("session", call, decision))
    await asyncio.sleep(0)
    approval = broker.pending("session")[0]

    assert broker.resolve(approval.id, True)
    assert await pending is True


@pytest.mark.asyncio
async def test_approval_can_be_remembered_for_session() -> None:
    broker = ApprovalBroker(EventBus())
    call = ToolCall(id="first", name="write_file", arguments={"path": "app.py"})
    decision = PolicyDecision(
        allowed=True,
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        reason="workspace modification",
    )
    first = asyncio.create_task(broker.request("session", call, decision))
    await asyncio.sleep(0)
    approval = broker.pending("session")[0]
    broker.resolve(approval.id, True, remember_for_session=True)
    assert await first is True

    reused = await broker.request(
        "session",
        call.model_copy(update={"id": "second"}),
        decision,
    )

    assert reused is True
    assert broker.pending("session") == []


@pytest.mark.asyncio
async def test_session_service_persists_messages_and_result(tmp_path: Path) -> None:
    model = FakeModel([ModelResponse(text="Analysis complete.")])
    service = SessionService(
        tmp_path / "state.sqlite3",
        model_factory=lambda _config: model,
    )

    result = await service.run_new(config(tmp_path), "Inspect the project.")

    assert result.status is AgentStatus.COMPLETED
    assert "available" in result.workspace_summary
    with SQLiteStorage(tmp_path / "state.sqlite3") as storage:
        sessions = storage.connection.execute("SELECT id FROM sessions").fetchall()
        session_id = sessions[0]["id"]
        messages = storage.list_messages(session_id)
        assert [record.message.role for record in messages] == [
            "system",
            "user",
            "assistant",
        ]
        assert storage.list_events(session_id)[-1].kind == "workspace_summary"
        assert any(
            event.kind == "run_finished" for event in storage.list_events(session_id)
        )


class SlowModel:
    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        timeout_s: float,
    ) -> ModelResponse:
        del messages, tools, timeout_s
        await asyncio.sleep(60)
        return ModelResponse(text="late")


@pytest.mark.asyncio
async def test_session_service_cancels_background_run(tmp_path: Path) -> None:
    service = SessionService(
        tmp_path / "state.sqlite3",
        model_factory=lambda _config: SlowModel(),
    )
    running = service.start_new(config(tmp_path), "Wait.")
    await asyncio.sleep(0)

    assert service.cancel(running.id)
    result = await running.task

    assert result.status is AgentStatus.CANCELLED


@pytest.mark.asyncio
async def test_running_clears_on_run_finished_before_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    released = asyncio.Event()

    async def blocked_summary(*_args: object, **_kwargs: object) -> dict[str, object]:
        await released.wait()
        return {
            "available": False,
            "summary": "blocked",
            "changed_entries": [],
            "changed_files": [],
            "insertions": 0,
            "deletions": 0,
            "untracked": 0,
            "error_code": None,
        }

    monkeypatch.setattr(
        "forge_agent.application.session_service.collect_workspace_summary",
        blocked_summary,
    )
    bus = EventBus()
    queue = bus.subscribe()
    service = SessionService(
        tmp_path / "state.sqlite3",
        events=bus,
        model_factory=lambda _config: FakeModel([ModelResponse(text="done")]),
    )
    running = service.start_new(config(tmp_path), "Finish quickly.")
    while True:
        event = await asyncio.wait_for(queue.get(), timeout=5)
        if event.kind == "run_finished" and event.session_id == running.id:
            break

    assert service.running(running.id) is None
    assert not running.task.done()
    released.set()
    result = await running.task
    assert result.status is AgentStatus.COMPLETED


@pytest.mark.asyncio
async def test_resume_marks_unfinished_tool_and_checks_workspace(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    with SQLiteStorage(database) as storage:
        storage.create_session(
            "resume",
            metadata={
                "workspace": tmp_path.as_posix(),
                "task": "resume",
                "mode": "build",
            },
        )
        storage.append_message("resume", Message(role="user", content="previous"))
        storage.append_event(
            "resume",
            "tool_started",
            {"call_id": "unfinished", "name": "read_file"},
        )
    service = SessionService(
        database,
        model_factory=lambda _config: FakeModel([ModelResponse(text="resumed")]),
    )

    result = await service.resume(config(tmp_path), "resume", "Continue.").task

    assert result.status is AgentStatus.COMPLETED
    with SQLiteStorage(database) as storage:
        interrupted = [
            event
            for event in storage.list_events("resume")
            if event.kind == "tool_interrupted"
        ]
        assert len(interrupted) == 1

    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="workspace differs"):
        service.resume(config(other), "resume", "Continue.")


@pytest.mark.asyncio
async def test_resume_repairs_assistant_tool_calls_without_results(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    with SQLiteStorage(database) as storage:
        storage.create_session(
            "resume-tools",
            metadata={
                "workspace": tmp_path.as_posix(),
                "task": "resume",
                "mode": "build",
            },
        )
        storage.append_message("resume-tools", Message(role="user", content="edit gitignore"))
        storage.append_message(
            "resume-tools",
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="call_missing",
                        name="write_file",
                        arguments={"path": ".gitignore", "content": "x"},
                    )
                ],
            ),
        )
    model = FakeModel([ModelResponse(text="repaired")])
    service = SessionService(
        database,
        model_factory=lambda _config: model,
    )

    result = await service.resume(config(tmp_path), "resume-tools", "Continue.").task

    assert result.status is AgentStatus.COMPLETED
    first = model.calls[0]
    assert any(
        message.role == "tool" and message.tool_call_id == "call_missing"
        for message in first
    )
    tool_index = next(
        index
        for index, message in enumerate(first)
        if message.role == "tool" and message.tool_call_id == "call_missing"
    )
    user_index = next(
        index
        for index, message in enumerate(first)
        if message.role == "user" and message.content == "Continue."
    )
    assert tool_index < user_index
    assert first[tool_index - 1].role == "assistant"
    assert first[tool_index - 1].tool_calls
    assert first[tool_index - 1].tool_calls[0].id == "call_missing"


def test_database_schema_version_is_recorded(tmp_path: Path) -> None:
    with SQLiteStorage(tmp_path / "state.sqlite3") as storage:
        version = storage.connection.execute("PRAGMA user_version").fetchone()[0]

    assert version == 1
