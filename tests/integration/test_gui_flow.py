import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from forge_agent.application import EventBus, SessionService
from forge_agent.config import RunConfig
from forge_agent.gui.viewmodels import event_to_view
from forge_agent.model.fake import FakeModel
from forge_agent.storage import SQLiteStorage
from forge_agent.types import AgentStatus, ModelResponse, RunMode, ToolCall


def tool_response(call_id: str, name: str, **arguments: Any) -> ModelResponse:
    return ModelResponse(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)]
    )


@pytest.mark.asyncio
async def test_fake_model_drives_gui_diff_and_evidence_panels(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")
    verify = 'python -B -c "from app import answer; assert answer() == 42"'
    model = FakeModel(
        [
            tool_response(
                "edit",
                "replace_in_file",
                path="app.py",
                old_text="return 41",
                new_text="return 42",
            ),
            tool_response("verify", "verify_changes", command=verify),
            ModelResponse(text="Fixed and verified the result."),
        ]
    )
    events = EventBus()
    queue = events.subscribe()
    database = tmp_path / "gui.sqlite3"
    service = SessionService(
        database,
        events=events,
        model_factory=lambda _config: model,
    )
    config = RunConfig(
        workspace=tmp_path,
        model="fake",
        api_key=SecretStr("test"),
        auto_approve=True,
    )

    running = service.start_new(config, "Fix answer and verify it.")
    result = await running.task
    views = []
    while not queue.empty():
        views.append(event_to_view(queue.get_nowait(), workspace=tmp_path))

    assert result.status is AgentStatus.COMPLETED
    assert any(view.diff and "+    return 42" in view.diff for view in views)
    assert any(
        view.kind == "tool_finished" and view.title.startswith("验证") and view.tone == "success"
        for view in views
    )
    with SQLiteStorage(database) as storage:
        claims = storage.list_claims(running.id)
        assert claims
        assert any(claim.status == "proven" for claim in claims)


@pytest.mark.asyncio
async def test_gui_approval_pauses_without_blocking_event_loop(tmp_path: Path) -> None:
    model = FakeModel(
        [
            tool_response(
                "write",
                "write_file",
                path="approved.txt",
                content="approved\n",
            ),
            tool_response(
                "verify",
                "verify_changes",
                command=(
                    'python -B -c "from pathlib import Path; '
                    "assert Path('approved.txt').exists()\""
                ),
            ),
            ModelResponse(text="The approved file was created."),
        ]
    )
    service = SessionService(
        tmp_path / "approval.sqlite3",
        model_factory=lambda _config: model,
    )
    config = RunConfig(
        workspace=tmp_path,
        model="fake",
        api_key=SecretStr("test"),
        auto_approve=False,
    )

    running = service.start_new(config, "Create approved.txt.")
    pending = []
    for _ in range(100):
        pending = service.approvals.pending(running.id)
        if pending:
            break
        await asyncio.sleep(0.01)

    assert pending
    assert not running.task.done()
    assert service.approvals.resolve(pending[0].id, True)
    second = []
    for _ in range(100):
        second = service.approvals.pending(running.id)
        if second and second[0].id != pending[0].id:
            break
        await asyncio.sleep(0.01)
    assert second
    assert service.approvals.resolve(second[0].id, True)
    result = await running.task

    assert result.status is AgentStatus.COMPLETED
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "approved\n"


@pytest.mark.asyncio
async def test_plan_mode_completes_without_execution_approval(tmp_path: Path) -> None:
    model = FakeModel(
        [
            ModelResponse(
                text="Goal: inspect the workspace.\nFeasibility: read-only.\nSuggestions: none."
            )
        ]
    )
    service = SessionService(
        tmp_path / "plan.sqlite3",
        model_factory=lambda _config: model,
    )
    config = RunConfig(
        workspace=tmp_path,
        model="fake",
        api_key=SecretStr("test"),
        mode=RunMode.PLAN,
        auto_approve=False,
    )

    result = await service.start_new(config, "分析工作区").task

    assert result.status is AgentStatus.COMPLETED
    assert "inspect the workspace" in result.summary
    assert service.approvals.pending() == []


@pytest.mark.asyncio
async def test_agent_plan_first_pauses_for_confirmation_then_implements(tmp_path: Path) -> None:
    model = FakeModel(
        [
            ModelResponse(
                text="Goal: add a marker file.\nFeasibility: safe.\nImplementation: write done.txt."
            ),
            ModelResponse(text="Implemented after confirmation."),
        ]
    )
    service = SessionService(
        tmp_path / "plan.sqlite3",
        model_factory=lambda _config: model,
    )
    config = RunConfig(
        workspace=tmp_path,
        model="fake",
        api_key=SecretStr("test"),
        mode=RunMode.BUILD,
        auto_approve=False,
    )

    running = service.start_new(config, "先给出方案，再执行")
    pending = []
    for _ in range(100):
        pending = service.approvals.pending(running.id)
        if pending:
            break
        await asyncio.sleep(0.01)

    assert pending
    assert pending[0].kind == "plan"
    assert not running.task.done()
    assert service.approvals.resolve(pending[0].id, True)
    result = await running.task

    assert result.status is AgentStatus.COMPLETED
    assert "Implemented after confirmation" in result.summary
