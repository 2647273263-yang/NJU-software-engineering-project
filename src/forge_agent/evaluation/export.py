"""JSON serialization with privacy-safe defaults."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from forge_agent.evaluation.models import EvaluationReport, EvaluationRun, JsonValue
from forge_agent.privacy.redaction import redact_data


def report_to_dict(
    report: EvaluationReport,
    *,
    redact: bool = True,
    anonymize_case_ids: bool = False,
    workspace: Path | None = None,
) -> dict[str, JsonValue]:
    """Convert a report to JSON data, optionally redacting and anonymizing it."""

    aliases = {
        run.case_id: f"anonymous-{index:03d}"
        for index, run in enumerate(report.runs, start=1)
    }
    runs: list[JsonValue] = [
        _run_to_dict(
            run,
            case_id=aliases[run.case_id] if anonymize_case_ids else run.case_id,
        )
        for run in report.runs
    ]
    data: dict[str, JsonValue] = {
        "summary": {
            "total_cases": report.total_cases,
            "completion_rate": report.completion_rate,
            "average_model_calls": report.average_model_calls,
            "average_steps": report.average_steps,
            "average_duration_s": report.average_duration_s,
            "recovery_rate": report.recovery_rate,
            "average_tokens": report.average_tokens,
        },
        "runs": runs,
    }
    if not redact:
        return data
    return cast(dict[str, JsonValue], redact_data(data, workspace=workspace))


def report_to_json(
    report: EvaluationReport,
    *,
    redact: bool = True,
    anonymize_case_ids: bool = False,
    workspace: Path | None = None,
    indent: int | None = 2,
) -> str:
    """Serialize a report as JSON."""

    data = report_to_dict(
        report,
        redact=redact,
        anonymize_case_ids=anonymize_case_ids,
        workspace=workspace,
    )
    return json.dumps(data, ensure_ascii=False, indent=indent, sort_keys=True)


def export_report_json(
    report: EvaluationReport,
    destination: str | Path,
    *,
    redact: bool = True,
    anonymize_case_ids: bool = False,
    workspace: Path | None = None,
    indent: int | None = 2,
) -> Path:
    """Write a report as UTF-8 JSON and return its destination."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        report_to_json(
            report,
            redact=redact,
            anonymize_case_ids=anonymize_case_ids,
            workspace=workspace,
            indent=indent,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _run_to_dict(run: EvaluationRun, *, case_id: str) -> dict[str, JsonValue]:
    return {
        "case_id": case_id,
        "completed": run.completed,
        "model_calls": run.model_calls,
        "steps": run.steps,
        "duration_s": run.duration_s,
        "resumed": run.resumed,
        "total_tokens": run.total_tokens,
        "output": run.output,
        "error": run.error,
        "metadata": dict(run.metadata),
    }
