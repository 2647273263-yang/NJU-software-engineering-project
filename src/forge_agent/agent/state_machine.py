"""Explicit, testable Agent status transitions."""

from __future__ import annotations

from forge_agent.types import AgentStatus

_TERMINAL = frozenset(
    {AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.CANCELLED, AgentStatus.STOPPED}
)

ALLOWED_TRANSITIONS: dict[AgentStatus, frozenset[AgentStatus]] = {
    AgentStatus.INITIALIZING: frozenset(
        {
            AgentStatus.THINKING,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
            AgentStatus.STOPPED,
        }
    ),
    AgentStatus.THINKING: frozenset(
        {
            AgentStatus.EXECUTING_TOOL,
            AgentStatus.VERIFYING,
            AgentStatus.AWAITING_APPROVAL,
            AgentStatus.AWAITING_PLAN_APPROVAL,
            AgentStatus.DEBUGGING,
            AgentStatus.COMPLETED,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
            AgentStatus.STOPPED,
        }
    ),
    AgentStatus.EXECUTING_TOOL: frozenset(
        {
            AgentStatus.THINKING,
            AgentStatus.VERIFYING,
            AgentStatus.AWAITING_APPROVAL,
            AgentStatus.AWAITING_PLAN_APPROVAL,
            AgentStatus.DEBUGGING,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
            AgentStatus.STOPPED,
        }
    ),
    AgentStatus.AWAITING_APPROVAL: frozenset(
        {
            AgentStatus.EXECUTING_TOOL,
            AgentStatus.THINKING,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
            AgentStatus.STOPPED,
        }
    ),
    AgentStatus.AWAITING_PLAN_APPROVAL: frozenset(
        {
            AgentStatus.THINKING,
            AgentStatus.EXECUTING_TOOL,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
            AgentStatus.STOPPED,
        }
    ),
    AgentStatus.VERIFYING: frozenset(
        {
            AgentStatus.THINKING,
            AgentStatus.DEBUGGING,
            AgentStatus.COMPLETED,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
            AgentStatus.STOPPED,
        }
    ),
    AgentStatus.DEBUGGING: frozenset(
        {
            AgentStatus.THINKING,
            AgentStatus.EXECUTING_TOOL,
            AgentStatus.VERIFYING,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
            AgentStatus.STOPPED,
        }
    ),
    AgentStatus.COMPLETED: frozenset(),
    AgentStatus.FAILED: frozenset(),
    AgentStatus.CANCELLED: frozenset(),
    AgentStatus.STOPPED: frozenset(),
}


class IllegalStateTransition(ValueError):
    def __init__(self, current: AgentStatus, target: AgentStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"illegal agent transition: {current.value} → {target.value}")


def can_transition(current: AgentStatus, target: AgentStatus) -> bool:
    if current is target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def transition(current: AgentStatus, target: AgentStatus) -> AgentStatus:
    if current is target:
        return current
    if not can_transition(current, target):
        raise IllegalStateTransition(current, target)
    return target


def is_terminal(status: AgentStatus) -> bool:
    return status in _TERMINAL
