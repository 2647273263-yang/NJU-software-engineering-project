from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge_agent.evaluation import (
    EvaluationCase,
    EvaluationContext,
    EvaluationOutcome,
    EvaluationRunner,
    agent_loop_sample_cases,
    deterministic_sample_cases,
    export_report_json,
    report_to_json,
    run_evaluation,
)


@pytest.mark.asyncio
async def test_runs_five_deterministic_anonymous_samples() -> None:
    cases = deterministic_sample_cases()

    report = await EvaluationRunner(max_concurrency=3).run(cases)

    assert len(cases) == 5
    assert [run.case_id for run in report.runs] == [
        "anonymous-001",
        "anonymous-002",
        "anonymous-003",
        "anonymous-004",
        "anonymous-005",
    ]
    assert all("anonymous" in case.tags for case in cases)


@pytest.mark.asyncio
async def test_aggregates_all_required_metrics() -> None:
    report = await run_evaluation(deterministic_sample_cases())

    assert report.total_cases == 5
    assert report.completion_rate == pytest.approx(0.6)
    assert report.average_model_calls == pytest.approx(1.4)
    assert report.average_steps == pytest.approx(2.0)
    assert report.average_duration_s >= 0
    assert report.recovery_rate == pytest.approx(0.5)
    assert report.average_tokens == pytest.approx(12.4)


@pytest.mark.asyncio
async def test_case_callable_can_run_without_model() -> None:
    async def execute(context: EvaluationContext) -> EvaluationOutcome:
        return EvaluationOutcome(
            completed=context.prompt == "local-only",
            steps=1,
            metadata={"mode": "callable"},
        )

    report = await run_evaluation(
        [EvaluationCase(case_id="callable-only", prompt="local-only", execute=execute)]
    )

    assert report.runs[0].completed
    assert report.runs[0].model_calls == 0
    assert report.runs[0].total_tokens == 0


@pytest.mark.asyncio
async def test_failure_is_captured_without_stopping_batch() -> None:
    async def fail(context: EvaluationContext) -> EvaluationOutcome:
        raise ValueError(f"bad task: {context.case_id}")

    cases = [
        EvaluationCase(case_id="broken", prompt="fail locally", execute=fail),
        deterministic_sample_cases()[0],
    ]

    report = await EvaluationRunner(max_concurrency=2).run(cases)

    assert not report.runs[0].completed
    assert report.runs[0].error == "ValueError: bad task: broken"
    assert report.runs[1].completed


@pytest.mark.asyncio
async def test_json_export_redacts_and_anonymizes(tmp_path: Path) -> None:
    async def expose_sensitive_data(context: EvaluationContext) -> EvaluationOutcome:
        del context
        return EvaluationOutcome(
            completed=True,
            steps=1,
            output="owner@example.com api_key=sk-examplevalue123",  # forge-release: allow
            metadata={"token": "private-value"},
        )

    report = await run_evaluation(
        [
            EvaluationCase(
                case_id="customer-project",
                prompt="offline",
                execute=expose_sensitive_data,
            )
        ]
    )
    destination = tmp_path / "nested" / "report.json"

    written = export_report_json(
        report,
        destination,
        redact=True,
        anonymize_case_ids=True,
    )
    payload = written.read_text(encoding="utf-8")

    assert written == destination
    assert "customer-project" not in payload
    assert "owner@example.com" not in payload  # forge-release: allow
    assert "sk-examplevalue123" not in payload
    assert "private-value" not in payload
    assert json.loads(payload)["runs"][0]["case_id"] == "anonymous-001"


@pytest.mark.asyncio
async def test_json_export_can_be_unredacted_for_trusted_local_use() -> None:
    async def execute(context: EvaluationContext) -> EvaluationOutcome:
        del context
        return EvaluationOutcome(completed=True, steps=1, output="user@example.com")  # forge-release: allow

    report = await run_evaluation(
        [EvaluationCase(case_id="visible-id", prompt="offline", execute=execute)]
    )

    payload = report_to_json(report, redact=False)

    assert "visible-id" in payload
    assert "user@example.com" in payload  # forge-release: allow


@pytest.mark.asyncio
async def test_rejects_duplicate_case_ids() -> None:
    case = deterministic_sample_cases()[0]

    with pytest.raises(ValueError, match="unique"):
        await EvaluationRunner().run([case, case])


@pytest.mark.asyncio
async def test_agent_loop_samples_stay_offline_and_cover_recovery() -> None:
    cases = agent_loop_sample_cases()
    report = await EvaluationRunner(max_concurrency=2).run(cases)

    assert [run.case_id for run in report.runs] == [
        "anonymous-loop-001",
        "anonymous-loop-002",
        "anonymous-loop-003",
    ]
    assert report.runs[0].completed
    assert report.runs[1].completed
    assert report.runs[1].resumed
    assert not report.runs[2].completed
    assert "same failing verification" in (report.runs[2].output or "")
