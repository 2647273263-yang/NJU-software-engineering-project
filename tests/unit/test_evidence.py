from forge_agent.agent.evidence import ClaimStatus, EvidenceLedger
from forge_agent.types import AgentStatus, RunResult, VerificationRecord


def test_retired_hypothesis_is_unverifiable() -> None:
    ledger = EvidenceLedger.from_run_result(
        RunResult(
            status=AgentStatus.STOPPED,
            summary="stopped",
            steps=2,
            model_calls=2,
            changed_files=["app.py"],
            verification=VerificationRecord(
                command="pytest",
                exit_code=1,
                workspace_version=1,
                duration_ms=4,
                output="AssertionError",
            ),
        ),
        events=[
            (
                "hypothesis_updated",
                {
                    "retired": True,
                    "hypothesis": "The latest workspace version still fails",
                    "observed_failure": "`pytest` exited 1",
                    "signature": "1|pytest|1",
                },
            )
        ],
    )

    statuses = [claim.status for claim in ledger.claims]
    assert ClaimStatus.UNVERIFIABLE in statuses


def test_workspace_summary_becomes_proven_when_git_is_available() -> None:
    ledger = EvidenceLedger.from_run_result(
        RunResult(
            status=AgentStatus.COMPLETED,
            summary="done",
            steps=1,
            model_calls=1,
            workspace_summary={
                "available": True,
                "changed_entries": ["app.py"],
                "untracked": 1,
                "insertions": 3,
                "deletions": 1,
            },
        )
    )

    claim = ledger.claims[-1]
    assert claim.status is ClaimStatus.PROVEN
    assert "untracked" in claim.evidence[0].description


def test_workspace_summary_omitted_when_git_is_unavailable() -> None:
    ledger = EvidenceLedger.from_run_result(
        RunResult(
            status=AgentStatus.COMPLETED,
            summary="done",
            steps=1,
            model_calls=1,
            workspace_summary={
                "available": False,
                "summary": "Git is not installed or not on PATH",
            },
        )
    )

    assert ledger.claims == []


def test_llm_judge_event_becomes_acceptance_claim() -> None:
    blocked = EvidenceLedger.from_run_result(
        RunResult(status=AgentStatus.COMPLETED, summary="done", steps=4, model_calls=4),
        events=[
            (
                "judge_finished",
                {
                    "accepted": False,
                    "reason": "quicksort is missing",
                    "missing": ["quicksort.py"],
                },
            ),
            (
                "judge_finished",
                {
                    "accepted": True,
                    "reason": "requested sorts are present",
                    "missing": [],
                },
            ),
        ],
    )
    claim = blocked.claims[-1]
    assert claim.statement == "LLM Judge accepted the run"
    assert claim.status is ClaimStatus.PROVEN
    assert claim.evidence[0].kind == "llm_judge"
