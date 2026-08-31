"""Hook configuration loaded from the workspace .forge directory."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class HookEvent(StrEnum):
    BEFORE_PROMPT = "before_prompt"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    STOP_ATTEMPTED = "stop_attempted"


class HookType(StrEnum):
    COMMAND = "command"
    PROMPT = "prompt"


class HookSpec(BaseModel):
    id: str
    event: HookEvent
    type: HookType = HookType.PROMPT
    enabled: bool = True
    timeout_s: float = Field(default=45.0, ge=1.0, le=120.0)
    prompt: str | None = None
    command: str | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        ident = value.strip()
        if not ident:
            raise ValueError("hook id is required")
        return ident


class HookConfig(BaseModel):
    llm_judge: bool = True
    block_dangerous_bash: bool = True
    block_secret_shell: bool = True
    max_judge_attempts: int = Field(default=2, ge=1, le=5)
    allow_command_hooks: bool = False
    hooks: list[HookSpec] = Field(default_factory=list)


BUILTIN_JUDGE = HookSpec(
    id="llm_judge",
    event=HookEvent.STOP_ATTEMPTED,
    type=HookType.PROMPT,
)

BUILTIN_DANGEROUS_BASH = HookSpec(
    id="block_dangerous_bash",
    event=HookEvent.BEFORE_TOOL,
    type=HookType.PROMPT,
)

BUILTIN_SECRET_SHELL = HookSpec(
    id="block_secret_shell",
    event=HookEvent.BEFORE_TOOL,
    type=HookType.PROMPT,
)


def load_hook_config(workspace: Path) -> HookConfig:
    forge = workspace / ".forge"
    for name in ("hooks.json", "hooks.yaml", "hooks.yml"):
        path = forge / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid hook config {path.name}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"hook config {path.name} must be a JSON object")
        return HookConfig.model_validate(data)
    return HookConfig()


def stop_hooks(config: HookConfig) -> list[HookSpec]:
    chosen: list[HookSpec] = []
    seen: set[str] = set()
    if config.llm_judge:
        chosen.append(BUILTIN_JUDGE)
        seen.add(BUILTIN_JUDGE.id)
    for spec in config.hooks:
        if not spec.enabled or spec.event is not HookEvent.STOP_ATTEMPTED:
            continue
        if spec.id in seen:
            chosen = [item if item.id != spec.id else spec for item in chosen]
            continue
        chosen.append(spec)
        seen.add(spec.id)
    return chosen


def before_tool_hooks(config: HookConfig) -> list[HookSpec]:
    chosen: list[HookSpec] = []
    seen: set[str] = set()
    if config.block_dangerous_bash:
        chosen.append(BUILTIN_DANGEROUS_BASH)
        seen.add(BUILTIN_DANGEROUS_BASH.id)
    if config.block_secret_shell:
        chosen.append(BUILTIN_SECRET_SHELL)
        seen.add(BUILTIN_SECRET_SHELL.id)
    for spec in config.hooks:
        if not spec.enabled or spec.event is not HookEvent.BEFORE_TOOL:
            continue
        if spec.id in seen:
            chosen = [item if item.id != spec.id else spec for item in chosen]
            continue
        chosen.append(spec)
        seen.add(spec.id)
    return chosen
