"""Read-only Git tools."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from forge_agent.tools.schemas import GitDiffArgs, GitStatusArgs
from forge_agent.tools.workspace import WorkspaceSandbox
from forge_agent.types import ToolResult


def _resolve_git() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(program_files) / "Git" / "cmd" / "git.exe",
        Path(program_files) / "Git" / "bin" / "git.exe",
        Path(program_files_x86) / "Git" / "cmd" / "git.exe",
        Path(local) / "Programs" / "Git" / "cmd" / "git.exe" if local else None,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return str(candidate)
    return None


class GitTools:
    def __init__(self, sandbox: WorkspaceSandbox, *, max_output_chars: int = 20_000) -> None:
        self.sandbox = sandbox
        self.max_output_chars = max_output_chars

    async def git_diff(self, args: GitDiffArgs) -> ToolResult:
        path = self.sandbox.resolve(args.path, must_exist=True)
        relative = self.sandbox.relative(path) or "."
        if args.ref is not None and args.ref.startswith("-"):
            raise ValueError("Git ref must not start with '-'")
        command = ["git", "-C", str(self.sandbox.root), "diff", "--no-ext-diff"]
        if args.staged:
            command.append("--cached")
        if args.ref:
            command.append(args.ref)
        command.extend(["--", relative])
        return await self._run(command, summary_prefix="git diff")

    async def git_status(self, args: GitStatusArgs) -> ToolResult:
        path = self.sandbox.resolve(args.path, must_exist=True)
        relative = self.sandbox.relative(path) or "."
        status = await self._capture(
            ["git", "-C", str(self.sandbox.root), "status", "--porcelain", "--", relative]
        )
        unstaged = await self._capture(
            ["git", "-C", str(self.sandbox.root), "diff", "--numstat", "--", relative]
        )
        staged = await self._capture(
            ["git", "-C", str(self.sandbox.root), "diff", "--cached", "--numstat", "--", relative]
        )
        if status.returncode != 0:
            unavailable = status.returncode == 127 or "was not found" in status.output
            output = status.output
            truncated = len(output) > self.max_output_chars
            if truncated:
                output = output[: self.max_output_chars] + "\n…"
            if unavailable:
                output = (
                    "Git is not installed or not on PATH. "
                    "Skip git tools and continue with list_files, read_file, and repo_outline."
                )
            return ToolResult(
                ok=False,
                summary=(
                    "Git is not installed or not on PATH"
                    if unavailable
                    else f"git status exited with code {status.returncode}"
                ),
                content=output,
                truncated=truncated,
                error_code="git_unavailable" if unavailable else "git_failed",
                metadata={"exit_code": status.returncode},
            )
        added, deleted = _sum_numstat(unstaged.output + "\n" + staged.output)
        untracked = sum(1 for line in status.output.splitlines() if line.startswith("??"))
        changed = [line[3:] for line in status.output.splitlines() if line.strip()]
        sections = [
            "# Git status",
            f"Path: {relative}",
            f"Changed entries: {len(changed)}",
            f"Untracked: {untracked}",
            f"Insertions: {added}",
            f"Deletions: {deleted}",
            "",
            "## Porcelain",
            status.output.strip() or "(clean)",
            "",
            "## Unstaged numstat",
            unstaged.output.strip() or "(none)",
            "",
            "## Staged numstat",
            staged.output.strip() or "(none)",
        ]
        content = "\n".join(sections)
        truncated = len(content) > self.max_output_chars
        if truncated:
            content = content[: self.max_output_chars] + "\n…"
        return ToolResult(
            ok=True,
            summary=(
                f"git status: {len(changed)} changed, {untracked} untracked, "
                f"+{added}/-{deleted}"
            ),
            content=content,
            truncated=truncated,
            metadata={
                "exit_code": 0,
                "changed_entries": changed,
                "untracked": untracked,
                "insertions": added,
                "deletions": deleted,
            },
        )

    async def _run(self, command: list[str], *, summary_prefix: str) -> ToolResult:
        captured = await self._capture(command)
        output = captured.output
        truncated = len(output) > self.max_output_chars
        if truncated:
            output = output[: self.max_output_chars] + "\n…"
        unavailable = captured.returncode == 127 or "was not found" in output
        return ToolResult(
            ok=captured.returncode == 0,
            summary=(
                "Git is not installed or not on PATH"
                if unavailable
                else f"{summary_prefix} exited with code {captured.returncode}"
            ),
            content=(
                "Git is not installed or not on PATH. "
                "Skip git tools and continue with list_files, read_file, and repo_outline."
                if unavailable
                else output
            ),
            truncated=truncated,
            error_code=(
                None
                if captured.returncode == 0
                else ("git_unavailable" if unavailable else "git_failed")
            ),
            metadata={"exit_code": captured.returncode},
        )

    async def _capture(self, command: list[str]) -> _Captured:
        resolved = list(command)
        if resolved and resolved[0] == "git":
            git = _resolve_git()
            if git is None:
                return _Captured(127, "git executable was not found")
            resolved[0] = git
        try:
            process = await asyncio.create_subprocess_exec(
                *resolved,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            executable = resolved[0] if resolved else "git"
            return _Captured(127, f"{executable} executable was not found")
        stdout, stderr = await process.communicate()
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")
        if error:
            output += ("\n" if output and not output.endswith("\n") else "") + error
        return _Captured(process.returncode if process.returncode is not None else -1, output)


async def collect_workspace_summary(
    workspace: Path,
    *,
    max_output_chars: int = 20_000,
) -> dict[str, Any]:
    """Collect a read-only working-tree snapshot for the end of a run."""

    tools = GitTools(WorkspaceSandbox(workspace), max_output_chars=max_output_chars)
    result = await tools.git_status(GitStatusArgs())
    metadata = result.metadata
    changed_entries = metadata.get("changed_entries", [])
    if not isinstance(changed_entries, list):
        changed_entries = []
    return {
        "available": result.ok,
        "summary": result.summary,
        "changed_entries": [str(item) for item in changed_entries],
        "untracked": int(metadata.get("untracked", 0) or 0),
        "insertions": int(metadata.get("insertions", 0) or 0),
        "deletions": int(metadata.get("deletions", 0) or 0),
        "error_code": result.error_code,
    }


class _Captured:
    def __init__(self, returncode: int, output: str) -> None:
        self.returncode = returncode
        self.output = output


def _sum_numstat(text: str) -> tuple[int, int]:
    added = 0
    deleted = 0
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        if parts[0] == "-" or parts[1] == "-":
            continue
        try:
            added += int(parts[0])
            deleted += int(parts[1])
        except ValueError:
            continue
    return added, deleted
