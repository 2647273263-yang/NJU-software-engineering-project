"""Validated argument models for built-in tools."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadFileArgs(ToolArguments):
    path: str
    max_chars: int | None = Field(default=None, ge=1)
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_line_range(self) -> ReadFileArgs:
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ListFilesArgs(ToolArguments):
    path: str = "."
    recursive: bool = False
    max_entries: int = Field(default=1_000, ge=1, le=20_000)


class SearchTextArgs(ToolArguments):
    query: str = Field(min_length=1)
    path: str = "."
    glob: str | None = None
    case_sensitive: bool = True
    max_matches: int = Field(default=100, ge=1, le=10_000)


class ReplaceInFileArgs(ToolArguments):
    path: str
    old_text: str = Field(min_length=1)
    new_text: str
    expected_replacements: int = Field(
        default=1,
        ge=1,
        le=10_000,
        description="How many exact matches to replace. Defaults to 1 (unique match).",
    )
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class WriteFileArgs(ToolArguments):
    path: str
    content: str
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    overwrite: bool = False


class DeleteFileArgs(ToolArguments):
    path: str


class UndoLastEditArgs(ToolArguments):
    pass


class RollbackChangesArgs(ToolArguments):
    pass


class RunCommandArgs(ToolArguments):
    command: str = Field(min_length=1)
    cwd: str = "."
    timeout_s: float | None = Field(default=None, gt=0, le=3_600)


class VerifyChangesArgs(ToolArguments):
    command: str | None = Field(default=None, min_length=1)
    cwd: str = "."
    timeout_s: float | None = Field(default=None, gt=0, le=3_600)


class GitDiffArgs(ToolArguments):
    path: str = "."
    staged: bool = False
    ref: str | None = None


class GitStatusArgs(ToolArguments):
    path: str = "."


class RepoOutlineArgs(ToolArguments):
    path: str = "."
    max_chars: int = Field(default=12_000, ge=200, le=50_000)
    query: str | None = None
    task: str | None = None


class SpawnExploreArgs(ToolArguments):
    task: str = Field(min_length=1, max_length=2_000)
    tools: list[str] | None = None
    model: str | None = None
    max_steps: int = Field(default=8, ge=1, le=8)
