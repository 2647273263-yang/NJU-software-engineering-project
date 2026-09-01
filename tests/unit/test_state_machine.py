from forge_agent.agent.state_machine import (
    IllegalStateTransition,
    can_transition,
    is_terminal,
    transition,
)
from forge_agent.types import AgentStatus


def test_allows_documented_runtime_path() -> None:
    status = AgentStatus.INITIALIZING
    for target in (
        AgentStatus.THINKING,
        AgentStatus.EXECUTING_TOOL,
        AgentStatus.AWAITING_APPROVAL,
        AgentStatus.EXECUTING_TOOL,
        AgentStatus.VERIFYING,
        AgentStatus.DEBUGGING,
        AgentStatus.STOPPED,
    ):
        status = transition(status, target)

    assert status is AgentStatus.STOPPED
    assert is_terminal(status)


def test_allows_more_tools_after_successful_verification() -> None:
    status = transition(AgentStatus.EXECUTING_TOOL, AgentStatus.VERIFYING)
    status = transition(status, AgentStatus.EXECUTING_TOOL)
    status = transition(status, AgentStatus.AWAITING_APPROVAL)
    assert status is AgentStatus.AWAITING_APPROVAL


def test_rejects_illegal_transition() -> None:
    assert not can_transition(AgentStatus.COMPLETED, AgentStatus.THINKING)
    try:
        transition(AgentStatus.FAILED, AgentStatus.THINKING)
    except IllegalStateTransition as exc:
        assert exc.current is AgentStatus.FAILED
        assert exc.target is AgentStatus.THINKING
    else:
        raise AssertionError("expected IllegalStateTransition")
