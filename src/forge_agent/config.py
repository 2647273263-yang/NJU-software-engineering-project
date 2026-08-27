"""Runtime configuration loaded from CLI options and environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, field_validator

from forge_agent.types import RunMode


class RunConfig(BaseModel):
    workspace: Path
    model: str
    api_key: SecretStr
    base_url: str | None = None
    mode: RunMode = RunMode.BUILD
    max_steps: int = Field(default=30, ge=1, le=100)
    max_model_calls: int = Field(default=30, ge=1, le=100)
    max_total_tokens: int = Field(default=1_000_000, ge=1_000)
    max_cost_usd: float | None = Field(default=None, gt=0)
    input_cost_per_million: float = Field(default=0.0, ge=0)
    output_cost_per_million: float = Field(default=0.0, ge=0)
    stream_model: bool = False
    command_timeout_s: float = Field(default=60.0, ge=1.0, le=3600.0)
    model_timeout_s: float = Field(default=90.0, ge=1.0, le=600.0)
    max_tool_output_chars: int = Field(default=20_000, ge=1_000, le=200_000)
    context_window: int = Field(default=128_000, ge=8_000)
    reserved_output_tokens: int = Field(default=8_000, ge=1_000)
    auto_approve: bool = False
    verify_command: str | None = None

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, value: Path) -> Path:
        workspace = value.expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"workspace is not a directory: {workspace}")
        return workspace

    @classmethod
    def from_environment(
        cls,
        workspace: Path,
        *,
        mode: RunMode = RunMode.BUILD,
        **overrides: object,
    ) -> RunConfig:
        api_key = os.environ.get("FORGE_API_KEY")
        model = os.environ.get("FORGE_MODEL")
        if not api_key:
            raise ValueError("FORGE_API_KEY is not set")
        if not model:
            raise ValueError("FORGE_MODEL is not set")
        return cls.model_validate(
            {
                "workspace": workspace,
                "api_key": SecretStr(api_key),
                "model": model,
                "base_url": os.environ.get("FORGE_BASE_URL"),
                "max_cost_usd": os.environ.get("FORGE_MAX_COST_USD"),
                "input_cost_per_million": os.environ.get(
                    "FORGE_INPUT_COST_PER_MILLION", 0
                ),
                "output_cost_per_million": os.environ.get(
                    "FORGE_OUTPUT_COST_PER_MILLION", 0
                ),
                "mode": mode,
                **overrides,
            }
        )
