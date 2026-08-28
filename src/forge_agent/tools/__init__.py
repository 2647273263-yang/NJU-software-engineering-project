"""Built-in tools and the default registry."""

from __future__ import annotations

from pathlib import Path

from forge_agent.tools.command import CommandTools
from forge_agent.tools.filesystem import FileTools
from forge_agent.tools.git import GitTools
from forge_agent.tools.registry import ToolRegistry, ToolSpec
from forge_agent.tools.repo import RepoTools
from forge_agent.tools.schemas import (
    GitDiffArgs,
    GitStatusArgs,
    ListFilesArgs,
    ReadFileArgs,
    ReplaceInFileArgs,
    RepoOutlineArgs,
    RollbackChangesArgs,
    RunCommandArgs,
    SearchTextArgs,
    UndoLastEditArgs,
    VerifyChangesArgs,
    WriteFileArgs,
)
from forge_agent.tools.workspace import WorkspaceSandbox, WorkspaceViolation

__all__ = [
    "ToolRegistry",
    "ToolSpec",
    "WorkspaceSandbox",
    "WorkspaceViolation",
    "build_default_registry",
]


def build_default_registry(
    workspace: Path,
    *,
    command_timeout_s: float = 60.0,
    max_output_chars: int = 20_000,
    suggested_verification: list[str] | None = None,
) -> ToolRegistry:
    sandbox = WorkspaceSandbox(workspace)
    files = FileTools(sandbox, max_output_chars=max_output_chars)
    commands = CommandTools(
        sandbox,
        default_timeout_s=command_timeout_s,
        max_output_chars=max_output_chars,
        suggested_commands=suggested_verification or (),
    )
    git = GitTools(sandbox, max_output_chars=max_output_chars)
    repo = RepoTools(sandbox, max_output_chars=max_output_chars)
    registry = ToolRegistry()
    specs = [
        ToolSpec("read_file", "Read a UTF-8 workspace file.", ReadFileArgs, files.read_file),
        ToolSpec("list_files", "List files within the workspace.", ListFilesArgs, files.list_files),
        ToolSpec("search_text", "Search UTF-8 files for text.", SearchTextArgs, files.search_text),
        ToolSpec(
            "replace_in_file",
            "Replace one unique text occurrence atomically.",
            ReplaceInFileArgs,
            files.replace_in_file,
        ),
        ToolSpec(
            "write_file",
            "Atomically write a UTF-8 workspace file.",
            WriteFileArgs,
            files.write_file,
        ),
        ToolSpec(
            "undo_last_edit",
            "Undo the latest agent edit if the file has not changed since.",
            UndoLastEditArgs,
            files.undo_last_edit,
        ),
        ToolSpec(
            "rollback_changes",
            "Roll back all unverified edits in the current edit group.",
            RollbackChangesArgs,
            files.rollback_changes,
        ),
        ToolSpec(
            "run_command",
            "Run a command asynchronously in the workspace.",
            RunCommandArgs,
            commands.run_command,
        ),
        ToolSpec(
            "verify_changes",
            "Run a test, lint, type-check, or build command as completion evidence.",
            VerifyChangesArgs,
            commands.verify_changes,
        ),
        ToolSpec("git_diff", "Show a repository diff.", GitDiffArgs, git.git_diff),
        ToolSpec(
            "git_status",
            "Show porcelain status, untracked files, and insertion/deletion counts.",
            GitStatusArgs,
            git.git_status,
        ),
        ToolSpec(
            "repo_outline",
            "Summarize repository languages, key paths, and Python symbols.",
            RepoOutlineArgs,
            repo.repo_outline,
        ),
    ]
    for spec in specs:
        registry.register(spec)
    return registry
