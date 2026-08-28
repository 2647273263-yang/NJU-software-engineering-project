"""Mutable run state and deterministic termination guards."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from forge_agent.agent.hypothesis import DebugHypothesis
from forge_agent.agent.state_machine import transition
from forge_agent.types import AgentStatus, ToolCall, VerificationRecord


@dataclass(slots=True)
class AgentState:
    status: AgentStatus = AgentStatus.INITIALIZING
    steps: int = 0
    model_calls: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    workspace_version: int = 0
    changed_files: set[str] = field(default_factory=set)
    verification: VerificationRecord | None = None
    completion_nudge_sent: bool = False
    consecutive_empty_responses: int = 0
    repeated_actions: Counter[str] = field(default_factory=Counter)
    last_error: str | None = None
    hypotheses: list[DebugHypothesis] = field(default_factory=list)

    def set_status(self, status: AgentStatus) -> None:
        self.status = transition(self.status, status)

    @property
    def needs_verification(self) -> bool:
        return bool(self.changed_files) and (
            self.verification is None
            or self.verification.workspace_version != self.workspace_version
            or not self.verification.passed
        )

    def record_tool_call(self, call: ToolCall) -> int:
        signature = json.dumps(
            {"name": call.name, "arguments": call.arguments},
            ensure_ascii=False,
            sort_keys=True,
        )
        self.repeated_actions[signature] += 1
        return self.repeated_actions[signature]

    def record_changes(self, paths: list[str]) -> None:
        if not paths:
            return
        self.workspace_version += 1
        self.changed_files.update(paths)

    def record_verification(self, verification: VerificationRecord) -> None:
        self.verification = verification

    def record_failed_verification(self, verification: VerificationRecord) -> DebugHypothesis:
        incoming = DebugHypothesis.from_verification(
            command=verification.command,
            exit_code=verification.exit_code,
            workspace_version=verification.workspace_version,
            output=verification.output,
        )
        existing = next(
            (item for item in self.hypotheses if item.signature == incoming.signature),
            None,
        )
        if existing is None:
            self.hypotheses.append(incoming)
            self.set_status(AgentStatus.DEBUGGING)
            return incoming
        existing.experiments += 1
        existing.last_result = incoming.last_result
        existing.retired = existing.experiments >= 2
        self.set_status(AgentStatus.DEBUGGING)
        return existing
