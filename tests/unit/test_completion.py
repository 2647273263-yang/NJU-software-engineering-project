from forge_agent.agent.completion import CompletionJudge
from forge_agent.agent.state import AgentState
from forge_agent.types import AgentStatus, VerificationRecord


def test_accepts_read_only_run() -> None:
    decision = CompletionJudge().evaluate(AgentState())

    assert decision.accepted
    assert decision.status is AgentStatus.COMPLETED


def test_rejects_stale_verification() -> None:
    state = AgentState(workspace_version=2, changed_files={"app.py"})
    state.verification = VerificationRecord(
        command="pytest",
        exit_code=0,
        workspace_version=1,
        duration_ms=10,
        output="passed",
    )

    decision = CompletionJudge().evaluate(state)

    assert not decision.accepted
    assert decision.status is AgentStatus.STOPPED
    assert "predates" in decision.reason
