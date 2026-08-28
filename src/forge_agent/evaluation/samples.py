"""Small deterministic scenarios for offline demos and smoke tests."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import SecretStr

from forge_agent.agent.loop import AgentLoop
from forge_agent.config import RunConfig
from forge_agent.evaluation.models import (
    CaseCallable,
    EvaluationCase,
    EvaluationContext,
    EvaluationOutcome,
    ModelFactory,
)
from forge_agent.model.base import ModelClient
from forge_agent.model.fake import FakeModel
from forge_agent.safety import PolicyEngine, PolicyToolRuntime
from forge_agent.tools import build_default_registry
from forge_agent.types import (
    AgentStatus,
    Message,
    ModelResponse,
    RunMode,
    TokenUsage,
    ToolCall,
    ToolResult,
)


@dataclass(frozen=True, slots=True)
class _SampleSpec:
    calls: int
    steps: int
    completed: bool
    resumed: bool
    tokens_per_call: int


def deterministic_sample_cases() -> tuple[EvaluationCase, ...]:
    """Return five anonymous cases that require no files, process, or network."""

    specs = (
        _SampleSpec(calls=1, steps=1, completed=True, resumed=False, tokens_per_call=8),
        _SampleSpec(calls=2, steps=2, completed=True, resumed=False, tokens_per_call=10),
        _SampleSpec(calls=1, steps=2, completed=False, resumed=False, tokens_per_call=6),
        _SampleSpec(calls=2, steps=3, completed=True, resumed=True, tokens_per_call=12),
        _SampleSpec(calls=1, steps=2, completed=False, resumed=True, tokens_per_call=4),
    )
    return tuple(_make_case(index, spec) for index, spec in enumerate(specs, start=1))


def _make_case(index: int, spec: _SampleSpec) -> EvaluationCase:
    responses = [
        ModelResponse(
            text=f"offline-step-{step}",
            usage=TokenUsage(
                input_tokens=spec.tokens_per_call - 2,
                output_tokens=2,
            ),
        )
        for step in range(1, spec.calls + 1)
    ]
    return EvaluationCase(
        case_id=f"anonymous-{index:03d}",
        prompt=f"Execute deterministic offline task {index}.",
        execute=_sample_executor(spec),
        model_factory=_fake_model_factory(responses),
        tags=("offline", "deterministic", "anonymous"),
    )


def _fake_model_factory(responses: list[ModelResponse]) -> ModelFactory:
    def create() -> ModelClient:
        return FakeModel(responses)

    return create


def _sample_executor(spec: _SampleSpec) -> CaseCallable:
    async def execute(context: EvaluationContext) -> EvaluationOutcome:
        if context.model is None:
            raise RuntimeError("sample case requires an injected model")
        output: str | None = None
        for call_index in range(spec.calls):
            response = await context.model.complete(
                [Message(role="user", content=f"{context.prompt} step={call_index + 1}")],
                [],
                timeout_s=1.0,
            )
            output = response.text
        return EvaluationOutcome(
            completed=spec.completed,
            steps=spec.steps,
            resumed=spec.resumed,
            output=output,
            metadata={"fixture": "offline"},
        )

    return execute


def agent_loop_sample_cases() -> tuple[EvaluationCase, ...]:
    """Return AgentLoop cases that stay offline and never touch the network."""

    return (
        EvaluationCase(
            case_id="anonymous-loop-001",
            prompt="Inspect the workspace and summarize.",
            execute=_inspect_executor(),
            model_factory=lambda: FakeModel([ModelResponse(text="No files needed changes.")]),
            tags=("offline", "anonymous", "agent-loop"),
        ),
        EvaluationCase(
            case_id="anonymous-loop-002",
            prompt="Fix add() and verify the change.",
            execute=_edit_verify_executor(),
            model_factory=_edit_verify_model_factory,
            tags=("offline", "anonymous", "agent-loop", "recovery"),
        ),
        EvaluationCase(
            case_id="anonymous-loop-003",
            prompt="Fix tests",
            execute=_repeated_failure_executor(),
            model_factory=_repeated_failure_model_factory,
            tags=("offline", "anonymous", "agent-loop"),
        ),
    )


def _workspace_config(workspace: Path) -> RunConfig:
    return RunConfig(
        workspace=workspace,
        model="fake",
        api_key=SecretStr("test"),
        auto_approve=True,
        max_steps=8,
        max_model_calls=8,
    )


def _inspect_executor() -> CaseCallable:
    async def execute(context: EvaluationContext) -> EvaluationOutcome:
        if context.model is None:
            raise RuntimeError("sample case requires an injected model")
        with TemporaryDirectory() as raw:
            workspace = Path(raw)
            result = await AgentLoop(
                config=_workspace_config(workspace),
                model=context.model,
                tools=PolicyToolRuntime(
                    build_default_registry(workspace, command_timeout_s=5),
                    PolicyEngine(mode=RunMode.BUILD, auto_approve=True),
                ),
            ).run(context.prompt)
        return EvaluationOutcome(
            completed=result.status is AgentStatus.COMPLETED,
            steps=result.steps,
            output=result.summary,
            metadata={"status": result.status.value, "fixture": "agent-loop"},
        )

    return execute


def _edit_verify_model_factory() -> ModelClient:
    verify_command = (
        f'"{sys.executable}" -B -c "from app import add; assert add(2, 3) == 5"'
    )
    return FakeModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="edit-1",
                        name="replace_in_file",
                        arguments={
                            "path": "app.py",
                            "old_text": "return left - right",
                            "new_text": "return left + right",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="verify-1",
                        name="verify_changes",
                        arguments={"command": verify_command},
                    )
                ]
            ),
            ModelResponse(text="Fixed add() and verified the result."),
        ]
    )


def _edit_verify_executor() -> CaseCallable:
    async def execute(context: EvaluationContext) -> EvaluationOutcome:
        if context.model is None:
            raise RuntimeError("sample case requires an injected model")
        with TemporaryDirectory() as raw:
            workspace = Path(raw)
            (workspace / "app.py").write_text(
                "def add(left: int, right: int) -> int:\n    return left - right\n",
                encoding="utf-8",
            )
            result = await AgentLoop(
                config=_workspace_config(workspace),
                model=context.model,
                tools=PolicyToolRuntime(
                    build_default_registry(workspace, command_timeout_s=5),
                    PolicyEngine(mode=RunMode.BUILD, auto_approve=True),
                ),
            ).run(context.prompt)
        recovered = bool(result.verification and result.verification.passed)
        return EvaluationOutcome(
            completed=result.status is AgentStatus.COMPLETED and recovered,
            steps=result.steps,
            resumed=recovered,
            output=result.summary,
            metadata={"status": result.status.value, "fixture": "agent-loop"},
        )

    return execute


class _ScriptedTools:
    def __init__(self, results: list[ToolResult]) -> None:
        self.results = results
        self.calls: list[ToolCall] = []

    def schemas(self) -> list[dict[str, Any]]:
        return []

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        return self.results[len(self.calls) - 1]


def _repeated_failure_model_factory() -> ModelClient:
    return FakeModel(
        [
            ModelResponse(tool_calls=[ToolCall(id="1", name="verify_changes")]),
            ModelResponse(tool_calls=[ToolCall(id="2", name="verify_changes")]),
            ModelResponse(text="should not be needed"),
        ]
    )


def _repeated_failure_executor() -> CaseCallable:
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

    async def execute(context: EvaluationContext) -> EvaluationOutcome:
        if context.model is None:
            raise RuntimeError("sample case requires an injected model")
        with TemporaryDirectory() as raw:
            result = await AgentLoop(
                config=_workspace_config(Path(raw)),
                model=context.model,
                tools=_ScriptedTools([failed, failed]),
            ).run(context.prompt)
        return EvaluationOutcome(
            completed=False,
            steps=result.steps,
            output=result.summary,
            metadata={"status": result.status.value, "fixture": "agent-loop"},
        )

    return execute
