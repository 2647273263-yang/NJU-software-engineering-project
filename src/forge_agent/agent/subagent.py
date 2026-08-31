"""Isolated read-only explore loop. Parent history only sees the conclusion."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from forge_agent.agent.loop import AgentLoop
from forge_agent.config import RunConfig
from forge_agent.context import ContextBudget, RuntimeContext
from forge_agent.hooks import HookRunner
from forge_agent.model.base import ModelClient
from forge_agent.safety import (
    READ_ONLY_TOOLS,
    SPAWN_EXPLORE,
    PolicyEngine,
    PolicyToolRuntime,
)
from forge_agent.tools import build_default_registry
from forge_agent.tools.registry import ToolRegistry, ToolSpec
from forge_agent.tools.schemas import SpawnExploreArgs
from forge_agent.types import AgentStatus, Message, RunMode, ToolCall, ToolResult

EventCallback = Callable[[str, dict[str, Any]], None]
ModelFactory = Callable[[RunConfig], ModelClient]

SPAWN_EXPLORE_DESCRIPTION = (
    "Explore the workspace in an isolated read-only sub-loop. "
    "Use for repository surveys, symbol search, or comparing implementations. "
    "Do not use this to write files, run tests, or execute shell commands. "
    "Returns a short conclusion and the relative paths that were read."
)
CONCLUSION_LIMIT = 2_000
MAX_EXPLORE_STEPS = 8
MAX_PATHS = 30
_SKIP_PARENT_EVENTS = frozenset(
    {
        "run_started",
        "run_finished",
        "context_prepared",
        "context_compacted",
        "memory_extracted",
        "memory_extract_failed",
        "workspace_summary",
    }
)


def register_spawn_explore(registry: ToolRegistry, handler: Any) -> None:
    registry.register(
        ToolSpec(
            SPAWN_EXPLORE,
            SPAWN_EXPLORE_DESCRIPTION,
            SpawnExploreArgs,
            handler,
        )
    )


def filter_explore_tools(
    source: ToolRegistry,
    names: frozenset[str],
) -> ToolRegistry:
    filtered = ToolRegistry()
    allowed = names & READ_ONLY_TOOLS
    for spec in source:
        if spec.name in allowed:
            filtered.register(spec)
    return filtered


def resolve_explore_tools(requested: list[str] | None) -> tuple[frozenset[str], list[str]]:
    if not requested:
        return frozenset(READ_ONLY_TOOLS), []
    allowed: set[str] = set()
    stripped: list[str] = []
    for name in requested:
        ident = name.strip()
        if ident in READ_ONLY_TOOLS:
            allowed.add(ident)
        elif ident:
            stripped.append(ident)
    if not allowed:
        return frozenset(READ_ONLY_TOOLS), stripped
    return frozenset(allowed), stripped


async def run_explore(
    *,
    parent: RunConfig,
    model_factory: ModelFactory,
    task: str,
    tools: list[str] | None = None,
    model: str | None = None,
    max_steps: int = MAX_EXPLORE_STEPS,
    on_event: EventCallback | None = None,
    parent_registry: ToolRegistry | None = None,
    project_context: str | None = None,
    user_rules: str | None = None,
) -> ToolResult:
    """Run a depth-1 explore loop and return one observation for the parent."""

    allowed, stripped = resolve_explore_tools(tools)
    source = parent_registry or build_default_registry(
        parent.workspace,
        command_timeout_s=parent.command_timeout_s,
        max_output_chars=parent.max_tool_output_chars,
    )
    registry = filter_explore_tools(source, allowed)
    steps = max(1, min(int(max_steps), MAX_EXPLORE_STEPS))
    chosen_model = (model or parent.model).strip() or parent.model
    sub_config = parent.model_copy(
        update={
            "model": chosen_model,
            "max_steps": steps,
            "max_model_calls": steps,
            "mode": RunMode.PLAN,
            "auto_approve": True,
            "verify_command": None,
            "stream_model": False,
        }
    )
    emit = on_event or (lambda _kind, _payload: None)

    def sub_on_event(kind: str, payload: dict[str, Any]) -> None:
        if kind in _SKIP_PARENT_EVENTS:
            return
        emit(kind, {**payload, "source": "subagent"})

    recorder = _PathRecorder(
        PolicyToolRuntime(
            registry,
            PolicyEngine(mode=RunMode.PLAN, auto_approve=True),
        )
    )
    sub_model = model_factory(sub_config)
    context = RuntimeContext(
        budget=ContextBudget(
            context_window=parent.context_window,
            reserved_output_tokens=parent.reserved_output_tokens,
        ),
        model=sub_model,
        project_context=project_context,
        user_rules=user_rules,
        retrieved_memory=None,
        max_tool_output_chars=parent.max_tool_output_chars,
        on_event=sub_on_event,
    )
    loop = AgentLoop(
        config=sub_config,
        model=sub_model,
        tools=recorder,
        context=context,
        on_event=sub_on_event,
        on_message=lambda _message: None,
        hooks=HookRunner.load(parent.workspace, sub_model, on_event=sub_on_event),
        explore=True,
    )
    result = await loop.run(task)
    conclusion = _conclusion_text(loop.messages, result.summary)
    truncated = len(conclusion) > CONCLUSION_LIMIT
    if truncated:
        conclusion = conclusion[:CONCLUSION_LIMIT].rstrip() + "…"
    paths = recorder.paths[:MAX_PATHS]
    notes: list[str] = []
    if stripped:
        notes.append("ignored tools: " + ", ".join(stripped))
    if truncated:
        notes.append("conclusion truncated")
    summary = conclusion or result.summary or "Explore finished without a conclusion."
    if notes:
        summary = f"{summary}\n[{'; '.join(notes)}]"
    content_lines = [f"paths_read: {', '.join(paths)}" if paths else "paths_read: (none)"]
    if notes:
        content_lines.extend(notes)
    return ToolResult(
        ok=result.status is AgentStatus.COMPLETED,
        summary=summary,
        content="\n".join(content_lines),
        truncated=truncated,
        metadata={
            "source": "subagent",
            "status": result.status.value,
            "steps": result.steps,
            "model_calls": result.model_calls,
            "total_tokens": result.total_tokens,
            "total_cost_usd": result.total_cost_usd,
            "paths_read": paths,
            "stripped_tools": stripped,
            "model": chosen_model,
        },
    )


def _conclusion_text(messages: list[Message], fallback: str) -> str:
    for message in reversed(messages):
        if message.role != "assistant":
            continue
        text = (message.content or "").strip()
        if text:
            return text
    return (fallback or "").strip()


class _PathRecorder:
    def __init__(self, inner: PolicyToolRuntime) -> None:
        self.inner = inner
        self.paths: list[str] = []

    def schemas(self) -> list[dict[str, Any]]:
        return self.inner.schemas()

    async def execute(self, call: ToolCall) -> ToolResult:
        result = await self.inner.execute(call)
        raw = call.arguments.get("path")
        if isinstance(raw, str) and raw.strip():
            posix = raw.replace("\\", "/")
            if posix not in self.paths:
                self.paths.append(posix)
        return result
