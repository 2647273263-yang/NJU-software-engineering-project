from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from forge_agent.agent import AgentLoop
from forge_agent.config import RunConfig
from forge_agent.model.fake import FakeModel
from forge_agent.safety import PolicyEngine, PolicyToolRuntime
from forge_agent.tools import build_default_registry
from forge_agent.types import AgentStatus, ModelResponse, RunMode, ToolCall


def tool_response(call_id: str, name: str, **arguments: Any) -> ModelResponse:
    return ModelResponse(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)]
    )


@pytest.mark.asyncio
async def test_end_to_end_edit_and_verify(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text(
        "def add(left: int, right: int) -> int:\n    return left - right\n",
        encoding="utf-8",
    )
    verify_command = (
        'python -c "from app import add; assert add(2, 3) == 5; print(\'verified\')"'
    )
    model = FakeModel(
        [
            tool_response("read-1", "read_file", path="app.py"),
            tool_response(
                "edit-1",
                "replace_in_file",
                path="app.py",
                old_text="return left - right",
                new_text="return left + right",
            ),
            tool_response(
                "verify-1",
                "verify_changes",
                command=verify_command,
            ),
            ModelResponse(
                text=(
                    "Fixed the arithmetic bug in app.py. "
                    "Verification passed with exit code 0."
                )
            ),
        ]
    )
    config = RunConfig(
        workspace=tmp_path,
        model="fake",
        api_key=SecretStr("test"),
        auto_approve=True,
    )
    registry = build_default_registry(tmp_path, command_timeout_s=5)
    tools = PolicyToolRuntime(
        registry,
        PolicyEngine(mode=RunMode.BUILD, auto_approve=True),
    )

    result = await AgentLoop(config=config, model=model, tools=tools).run(
        "Fix add() and verify it."
    )

    assert result.status is AgentStatus.COMPLETED
    assert result.changed_files == ["app.py"]
    assert result.verification is not None
    assert result.verification.passed
    assert "return left + right" in source.read_text(encoding="utf-8")


def test_plan_mode_only_advertises_read_only_tools(tmp_path: Path) -> None:
    runtime = PolicyToolRuntime(
        build_default_registry(tmp_path),
        PolicyEngine(mode=RunMode.PLAN),
    )

    names = {schema["function"]["name"] for schema in runtime.schemas()}

    assert {
        "read_file",
        "list_files",
        "search_text",
        "git_diff",
        "git_status",
        "repo_outline",
    } <= names
    assert "write_file" not in names
    assert "run_command" not in names
    assert "verify_changes" not in names


@pytest.mark.asyncio
async def test_failed_verification_then_second_edit_succeeds(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text(
        "def add(left: int, right: int) -> int:\n    return left - right\n",
        encoding="utf-8",
    )
    verify_command = 'python -B -c "from app import add; assert add(2, 3) == 5"'
    model = FakeModel(
        [
            tool_response(
                "bad-edit",
                "replace_in_file",
                path="app.py",
                old_text="return left - right",
                new_text="return left * right",
            ),
            tool_response("failed-check", "verify_changes", command=verify_command),
            tool_response(
                "fixed-edit",
                "replace_in_file",
                path="app.py",
                old_text="return left * right",
                new_text="return left + right",
            ),
            tool_response("passed-check", "verify_changes", command=verify_command),
            ModelResponse(text="Corrected the implementation after the failed check."),
        ]
    )
    config = RunConfig(
        workspace=tmp_path,
        model="fake",
        api_key=SecretStr("test"),
        auto_approve=True,
    )
    tools = PolicyToolRuntime(
        build_default_registry(tmp_path, command_timeout_s=5),
        PolicyEngine(mode=RunMode.BUILD, auto_approve=True),
    )

    result = await AgentLoop(config=config, model=model, tools=tools).run("Fix add.")

    assert result.status is AgentStatus.COMPLETED
    assert result.verification is not None
    assert result.verification.passed
    assert result.verification.workspace_version == 2
    assert "return left + right" in source.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_preferred_command_runs_automatically_after_edit(tmp_path: Path) -> None:
    source = tmp_path / "value.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    verify_command = 'python -B -c "from value import VALUE; assert VALUE == 2"'
    model = FakeModel(
        [
            tool_response(
                "edit",
                "replace_in_file",
                path="value.py",
                old_text="VALUE = 1",
                new_text="VALUE = 2",
            ),
            ModelResponse(text="The edit is complete."),
            ModelResponse(text="The automatic verification passed."),
        ]
    )
    config = RunConfig(
        workspace=tmp_path,
        model="fake",
        api_key=SecretStr("test"),
        auto_approve=True,
        verify_command=verify_command,
    )
    tools = PolicyToolRuntime(
        build_default_registry(tmp_path, command_timeout_s=5),
        PolicyEngine(mode=RunMode.BUILD, auto_approve=True),
    )
    events: list[tuple[str, dict[str, Any]]] = []

    result = await AgentLoop(
        config=config,
        model=model,
        tools=tools,
        on_event=lambda kind, payload: events.append((kind, payload)),
    ).run("Set VALUE to 2.")

    assert result.status is AgentStatus.COMPLETED
    assert result.verification is not None and result.verification.passed
    assert any(kind == "automatic_verification_started" for kind, _ in events)
