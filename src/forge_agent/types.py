"""Provider-neutral runtime types used by the agent."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentStatus(StrEnum):
    INITIALIZING = "initializing"
    THINKING = "thinking"
    EXECUTING_TOOL = "executing_tool"
    VERIFYING = "verifying"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    DEBUGGING = "debugging"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STOPPED = "stopped"


class RunMode(StrEnum):
    PLAN = "plan"
    BUILD = "build"


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str | None = None


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ModelResponse(BaseModel):
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    request_id: str | None = None


class ToolResult(BaseModel):
    ok: bool
    summary: str
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False
    duration_ms: int = 0
    error_code: str | None = None

    def as_observation(self) -> str:
        parts = [self.summary]
        if self.content:
            parts.append(self.content)
        if self.truncated:
            parts.append("[output truncated]")
        if self.error_code:
            parts.append(f"[error_code={self.error_code}]")
        return "\n".join(parts)


class VerificationRecord(BaseModel):
    command: str
    exit_code: int
    workspace_version: int
    duration_ms: int
    output: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


class RunResult(BaseModel):
    status: AgentStatus
    summary: str
    steps: int
    model_calls: int
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    changed_files: list[str] = Field(default_factory=list)
    verification: VerificationRecord | None = None
    workspace_summary: dict[str, Any] = Field(default_factory=dict)
