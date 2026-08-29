"""ForgeAgent command-line entry point."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from platformdirs import user_data_path
from rich.console import Console
from rich.table import Table

from forge_agent.application import (
    ApplicationEvent,
    EventBus,
    RunningSession,
    SessionService,
)
from forge_agent.application.replay import replay_delay_seconds
from forge_agent.config import RunConfig
from forge_agent.evaluation import (
    agent_loop_sample_cases,
    deterministic_sample_cases,
    export_report_json,
    run_evaluation,
)
from forge_agent.privacy import export_events_jsonl, scan_repository
from forge_agent.safety import PolicyDecision
from forge_agent.storage import SQLiteStorage
from forge_agent.types import AgentStatus, RunMode, RunResult, ToolCall
from forge_agent.ui import ConsoleRenderer

app = typer.Typer(
    name="forge",
    help="A small, inspectable local coding agent.",
    no_args_is_help=True,
)
console = Console()


def _database_path() -> Path:
    return user_data_path("forge-agent", ensure_exists=True) / "sessions.sqlite3"


def _approve_in_terminal(call: ToolCall, decision: PolicyDecision) -> bool:
    if call.name == "propose_plan":
        plan = str(call.arguments.get("plan") or "")
        if plan:
            console.print(plan)
        return typer.confirm(
            "Approve this plan and continue in Agent/Build mode?",
            default=False,
        )
    console.print(
        f"[yellow]Approval required[/yellow] · {call.name} · "
        f"{decision.risk.value}: {decision.reason}"
    )
    console.print(call.arguments)
    return typer.confirm("Allow this operation?", default=False)


def _plan_aware_approver(build_after_plan: bool):
    def handler(call: ToolCall, decision: PolicyDecision) -> bool:
        if call.name == "propose_plan" and build_after_plan:
            return True
        return _approve_in_terminal(call, decision)

    return handler


async def _render_session(
    running: RunningSession,
    queue: asyncio.Queue[ApplicationEvent],
    renderer: ConsoleRenderer,
) -> RunResult:
    console.print(f"[dim]Session: {running.id}[/dim]")
    while not running.task.done() or not queue.empty():
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.1)
        except TimeoutError:
            continue
        if event.session_id == running.id:
            renderer.event(event.kind, event.payload)
    result = await running.task
    renderer.result(result)
    return result


@app.command()
def run(
    task: Annotated[str, typer.Argument(help="Programming task for the agent.")],
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Workspace directory."),
    ] = Path("."),
    mode: Annotated[
        RunMode,
        typer.Option("--mode", help="Permission mode: plan or build."),
    ] = RunMode.BUILD,
    verify: Annotated[
        str | None,
        typer.Option("--verify", help="Preferred verification command."),
    ] = None,
    auto_approve: Annotated[
        bool,
        typer.Option("--auto-approve", help="Approve medium-risk tool calls."),
    ] = False,
    max_steps: Annotated[
        int,
        typer.Option("--max-steps", min=1, max=100),
    ] = 30,
    max_tokens: Annotated[
        int,
        typer.Option("--max-tokens", min=1_000, help="Stop after this many model tokens."),
    ] = 1_000_000,
    max_cost: Annotated[
        float | None,
        typer.Option("--max-cost", min=0.0001, help="Optional USD cost budget."),
    ] = None,
    stream: Annotated[
        bool,
        typer.Option("--stream/--no-stream", help="Stream model text when supported."),
    ] = False,
    build_after_plan: Annotated[
        bool,
        typer.Option(
            "--build-after-plan",
            help="After a plan is produced, continue in build mode without asking again.",
        ),
    ] = False,
) -> None:
    """Run a new coding-agent session."""

    try:
        config = RunConfig.from_environment(
            workspace,
            mode=mode,
            verify_command=verify,
            auto_approve=auto_approve,
            max_steps=max_steps,
            max_total_tokens=max_tokens,
            max_cost_usd=max_cost,
            stream_model=stream,
        )
    except ValueError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(2) from exc

    async def execute() -> RunResult:
        events = EventBus()
        queue = events.subscribe()
        service = SessionService(
            _database_path(),
            events=events,
            approval_handler=_plan_aware_approver(build_after_plan),
        )
        renderer = ConsoleRenderer(console)
        console.print(f"[dim]Workspace: {config.workspace.as_posix()}[/dim]")
        running = service.start_new(config, task)
        result = await _render_session(running, queue, renderer)
        events.unsubscribe(queue)
        return result

    result = asyncio.run(execute())
    if result.status not in {AgentStatus.COMPLETED, AgentStatus.STOPPED}:
        raise typer.Exit(1)


@app.command()
def resume(
    session_id: Annotated[str, typer.Argument(help="Session identifier.")],
    instruction: Annotated[
        str,
        typer.Option("--instruction", "-i", help="Additional instruction for the resumed run."),
    ] = "Continue the previous task from the persisted history.",
    auto_approve: Annotated[
        bool,
        typer.Option("--auto-approve", help="Approve medium-risk tool calls."),
    ] = False,
) -> None:
    """Resume a session from its persisted conversation history."""

    with SQLiteStorage(_database_path()) as storage:
        session = storage.get_session(session_id)
        if session is None:
            console.print(f"[red]Unknown session:[/red] {session_id}")
            raise typer.Exit(2)
        metadata = dict(session.metadata)
    try:
        config = RunConfig.from_environment(
            Path(str(metadata["workspace"])),
            mode=RunMode(str(metadata.get("mode", RunMode.BUILD.value))),
            verify_command=metadata.get("verify_command"),
            auto_approve=auto_approve,
        )
    except (KeyError, ValueError) as exc:
        console.print(f"[red]Cannot resume session:[/red] {exc}")
        raise typer.Exit(2) from exc

    async def execute() -> RunResult:
        events = EventBus()
        queue = events.subscribe()
        service = SessionService(
            _database_path(),
            events=events,
            approval_handler=_approve_in_terminal,
        )
        running = service.resume(config, session_id, instruction)
        result = await _render_session(running, queue, ConsoleRenderer(console))
        events.unsubscribe(queue)
        return result

    result = asyncio.run(execute())
    if result.status not in {AgentStatus.COMPLETED, AgentStatus.STOPPED}:
        raise typer.Exit(1)


@app.command("sessions")
def list_sessions() -> None:
    """List locally persisted sessions."""

    with SQLiteStorage(_database_path()) as storage:
        rows = storage.connection.execute(
            "SELECT id, created_at, updated_at, metadata_json "
            "FROM sessions ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
    table = Table("Session", "Created", "Status", "Task")
    for row in rows:
        metadata = json.loads(row["metadata_json"])
        table.add_row(
            row["id"],
            row["created_at"],
            str(metadata.get("status", "unknown")),
            str(metadata.get("task", ""))[:80],
        )
    console.print(table)


@app.command()
def inspect(
    session_id: Annotated[str, typer.Argument(help="Session identifier.")],
) -> None:
    """Inspect the event trajectory of a session."""

    with SQLiteStorage(_database_path()) as storage:
        session = storage.get_session(session_id)
        if session is None:
            console.print(f"[red]Unknown session:[/red] {session_id}")
            raise typer.Exit(2)
        events = storage.list_events(session_id)
        compactions = storage.list_compactions(session_id)
        claims = [
            (claim, storage.list_evidence(claim.id))
            for claim in storage.list_claims(session_id)
        ]
    table = Table("Time", "Event", "Details")
    for event in events:
        details = ", ".join(f"{key}={value}" for key, value in event.payload.items())
        table.add_row(event.created_at, event.kind, details[:160])
    console.print(table)
    if compactions:
        context_table = Table("Compaction", "Through message", "Summary")
        for compaction in compactions:
            context_table.add_row(
                str(compaction.id),
                str(compaction.through_message_id),
                json.dumps(compaction.summary, ensure_ascii=False)[:240],
            )
        console.print(context_table)
    if claims:
        evidence_table = Table("Status", "Claim", "Evidence")
        for claim, evidence in claims:
            evidence_table.add_row(
                claim.status,
                claim.statement,
                "\n".join(item.description for item in evidence) or "none",
            )
        console.print(evidence_table)


@app.command()
def replay(
    session_id: Annotated[str, typer.Argument(help="Session identifier.")],
    speed: Annotated[
        float,
        typer.Option(
            "--speed",
            min=0.0,
            help="Playback speed. 0 dumps instantly; 1 follows original timing.",
        ),
    ] = 0.0,
) -> None:
    """Replay saved events without re-executing tools or model calls."""

    renderer = ConsoleRenderer(console)
    with SQLiteStorage(_database_path()) as storage:
        session = storage.get_session(session_id)
        if session is None:
            console.print(f"[red]Unknown session:[/red] {session_id}")
            raise typer.Exit(2)
        events = storage.list_events(session_id)
    console.print(f"[bold]Replay {session_id}[/bold]")
    previous: str | None = None
    for event in events:
        delay = replay_delay_seconds(previous, event.created_at, speed)
        if delay:
            time.sleep(delay)
        previous = event.created_at
        console.print(f"[dim]{event.created_at} · {event.kind}[/dim]")
        renderer.event(event.kind, event.payload)


@app.command()
def rollback(
    session_id: Annotated[str, typer.Argument(help="Session identifier.")],
) -> None:
    """Roll back unverified edits for a persisted session."""

    with SQLiteStorage(_database_path()) as storage:
        session = storage.get_session(session_id)
        if session is None:
            console.print(f"[red]Unknown session:[/red] {session_id}")
            raise typer.Exit(2)
        metadata = dict(session.metadata)
    try:
        config = RunConfig.from_environment(
            Path(str(metadata["workspace"])),
            mode=RunMode(str(metadata.get("mode", RunMode.BUILD.value))),
            auto_approve=True,
        )
    except (KeyError, ValueError) as exc:
        console.print(f"[red]Cannot roll back session:[/red] {exc}")
        raise typer.Exit(2) from exc

    result = asyncio.run(SessionService(_database_path()).rollback_changes(config, session_id))
    if result.ok:
        console.print(f"[green]{result.summary}[/green]")
        return
    console.print(f"[red]{result.summary}[/red]")
    raise typer.Exit(1)


@app.command("eval")
def evaluate(
    destination: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional JSON report path."),
    ] = None,
    concurrency: Annotated[
        int,
        typer.Option("--concurrency", min=1, max=8),
    ] = 2,
) -> None:
    """Run the offline FakeModel evaluation suite without network access."""

    cases = [*deterministic_sample_cases(), *agent_loop_sample_cases()]
    report = asyncio.run(run_evaluation(cases, max_concurrency=concurrency))
    table = Table("Metric", "Value")
    table.add_row("Cases", str(report.total_cases))
    table.add_row("Completion rate", f"{report.completion_rate:.0%}")
    table.add_row("Recovery rate", f"{report.recovery_rate:.0%}")
    table.add_row("Average model calls", f"{report.average_model_calls:.2f}")
    table.add_row("Average steps", f"{report.average_steps:.2f}")
    table.add_row("Average tokens", f"{report.average_tokens:.1f}")
    table.add_row("Average duration (s)", f"{report.average_duration_s:.3f}")
    console.print(table)
    if destination is not None:
        export_report_json(report, destination, anonymize_case_ids=True)
        console.print(f"[green]Wrote redacted report[/green] to {destination}")


@app.command()
def doctor() -> None:
    """Check local configuration without sending a model request."""

    checks = [
        (
            "FORGE_API_KEY",
            bool(os.environ.get("FORGE_API_KEY")),
            "configured" if os.environ.get("FORGE_API_KEY") else "missing",
        ),
        (
            "FORGE_MODEL",
            bool(os.environ.get("FORGE_MODEL")),
            os.environ.get("FORGE_MODEL", "missing"),
        ),
        (
            "FORGE_BASE_URL",
            True,
            os.environ.get("FORGE_BASE_URL", "provider default"),
        ),
        ("Python", True, sys.version.split()[0]),
        ("Git", shutil.which("git") is not None, shutil.which("git") or "not found"),
        ("ripgrep", shutil.which("rg") is not None, shutil.which("rg") or "Python fallback"),
        ("Session database", True, str(_database_path())),
    ]
    table = Table("Check", "Status", "Detail")
    for name, ok, detail in checks:
        table.add_row(
            name,
            "[green]ok[/green]" if ok else "[yellow]warning[/yellow]",
            detail,
        )
    console.print(table)


@app.command("release-check")
def release_check(
    repository: Annotated[
        Path,
        typer.Argument(help="Repository directory to scan before publication."),
    ] = Path("."),
) -> None:
    """Scan a repository for credentials and identifying material."""

    result = scan_repository(repository.resolve())
    table = Table("Severity", "Kind", "Path", "Line", "Detector")
    for finding in result.findings:
        table.add_row(
            finding.severity.value,
            finding.kind.value,
            finding.path,
            str(finding.line or ""),
            finding.detector,
        )
    if result.findings:
        console.print(table)
        console.print(
            f"[red]{len(result.findings)} privacy risk(s) found.[/red] "
            "Matched secret values are intentionally not displayed."
        )
        raise typer.Exit(1)
    console.print(
        f"[green]Release check passed.[/green] "
        f"{result.files_scanned} files scanned, {result.files_skipped} skipped."
    )


@app.command("export-events")
def export_events(
    session_id: Annotated[str, typer.Argument(help="Session identifier.")],
    destination: Annotated[
        Path,
        typer.Argument(help="Destination JSONL file."),
    ],
) -> None:
    """Export a redacted event trajectory as JSONL."""

    with SQLiteStorage(_database_path()) as storage:
        session = storage.get_session(session_id)
        if session is None:
            console.print(f"[red]Unknown session:[/red] {session_id}")
            raise typer.Exit(2)
        events = storage.list_events(session_id)
        workspace = Path(str(session.metadata.get("workspace", ".")))
    count = export_events_jsonl(events, destination, workspace=workspace)
    console.print(f"[green]Exported {count} redacted event(s)[/green] to {destination}")


if __name__ == "__main__":
    app()
