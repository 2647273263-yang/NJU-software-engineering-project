"""Compact terminal renderer for agent events and final evidence."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from forge_agent.agent.evidence import EvidenceLedger
from forge_agent.privacy import redact_data
from forge_agent.types import RunResult


class ConsoleRenderer:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        safe = redact_data(payload)
        if event_type == "tool_started":
            self.console.print(f"[cyan]→[/cyan] {safe['name']} {safe.get('arguments', {})}")
        elif event_type == "tool_finished":
            marker = "[green]✓[/green]" if safe.get("ok") else "[red]✗[/red]"
            self.console.print(
                f"{marker} {safe['name']}  {safe.get('duration_ms', 0)} ms"
                + (
                    f"  [red]{safe['error_code']}[/red]"
                    if safe.get("error_code")
                    else ""
                )
            )
        elif event_type == "verification_required":
            self.console.print("[yellow]The latest changes still need verification.[/yellow]")
        elif event_type == "context_overflow_recovery":
            self.console.print("[yellow]Context overflow: compacting and retrying.[/yellow]")
        elif event_type == "context_compacted":
            self.console.print(
                "[magenta]Context compacted[/magenta] · "
                f"{safe.get('messages_compacted', 0)} messages · "
                f"{safe.get('estimated_tokens_before', 0)} → "
                f"{safe.get('estimated_tokens_after', 0)} estimated tokens"
            )
        elif event_type == "model_response":
            text = str(safe.get("text") or "").strip()
            tool_calls = int(safe.get("tool_calls") or 0)
            if text and tool_calls == 0:
                self.console.print(Panel(text, title="Answer"))
            else:
                self.console.print(
                    f"[dim]Model response · {safe.get('tokens', 0)} tokens · "
                    f"{tool_calls} tool call(s)[/dim]"
                )
        elif event_type == "hypothesis_updated":
            retired = (
                "retired"
                if safe.get("retired")
                else f"experiment {safe.get('experiments', 1)}"
            )
            self.console.print(
                f"[yellow]Debug hypothesis[/yellow] · {retired} · "
                f"{safe.get('observed_failure', '')}"
            )
        elif event_type == "automatic_verification_started":
            self.console.print(
                f"[cyan]Automatic verification[/cyan] · {safe.get('commands', [])}"
            )
        elif event_type == "automatic_verification_finished":
            passed = "passed" if safe.get("passed") else "failed"
            self.console.print(f"[cyan]Automatic verification[/cyan] · {passed}")
        elif event_type == "workspace_summary":
            if safe.get("available"):
                self.console.print(
                    "[blue]Working tree[/blue] · "
                    f"{len(safe.get('changed_entries', []) or [])} changed · "
                    f"+{safe.get('insertions', 0)}/-{safe.get('deletions', 0)} · "
                    f"{safe.get('untracked', 0)} untracked"
                )
            else:
                self.console.print(
                    f"[blue]Working tree[/blue] · unavailable · {safe.get('summary', '')}"
                )
        elif event_type == "unhandled_error":
            self.console.print(f"[red]Unhandled error:[/red] {safe.get('error')}")

    def result(self, result: RunResult) -> None:
        self.console.print(
            Panel(
                result.summary,
                title=f"ForgeAgent · {result.status.value}",
                subtitle=(
                    f"{result.steps} steps · {result.model_calls} model calls · "
                    f"{result.total_tokens} tokens"
                ),
            )
        )
        ledger = EvidenceLedger.from_run_result(result)
        if not ledger.claims:
            return
        table = Table(title="Completion evidence")
        table.add_column("Status")
        table.add_column("Claim")
        table.add_column("Evidence")
        for claim in ledger.claims:
            evidence = "\n".join(item.description for item in claim.evidence) or "none"
            table.add_row(claim.status.value, claim.statement, evidence)
        self.console.print(table)
