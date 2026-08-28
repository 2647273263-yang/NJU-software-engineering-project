"""Deterministic completion policy independent from model self-assessment."""

from __future__ import annotations

from dataclasses import dataclass

from forge_agent.agent.state import AgentState
from forge_agent.types import AgentStatus


@dataclass(frozen=True, slots=True)
class CompletionDecision:
    status: AgentStatus
    accepted: bool
    reason: str


class CompletionJudge:
    def evaluate(self, state: AgentState) -> CompletionDecision:
        if not state.changed_files:
            return CompletionDecision(
                status=AgentStatus.COMPLETED,
                accepted=True,
                reason="No workspace modifications require verification.",
            )
        if state.verification is None:
            return CompletionDecision(
                status=AgentStatus.STOPPED,
                accepted=False,
                reason="Workspace changes have no verification evidence.",
            )
        if state.verification.workspace_version != state.workspace_version:
            return CompletionDecision(
                status=AgentStatus.STOPPED,
                accepted=False,
                reason="Verification evidence predates the latest workspace change.",
            )
        if not state.verification.passed:
            return CompletionDecision(
                status=AgentStatus.STOPPED,
                accepted=False,
                reason=(
                    f"Verification failed with exit code {state.verification.exit_code}."
                ),
            )
        return CompletionDecision(
            status=AgentStatus.COMPLETED,
            accepted=True,
            reason="Latest workspace version has successful verification evidence.",
        )
