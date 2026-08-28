"""Application service shared by command-line and graphical frontends."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge_agent.agent import AgentLoop
from forge_agent.agent.evidence import EvidenceLedger
from forge_agent.agent.tool_runtime import PersistentToolRuntime
from forge_agent.application.approval import ApprovalBroker
from forge_agent.application.events import EventBus
from forge_agent.config import RunConfig
from forge_agent.context import (
    CompactionSummary,
    ContextBudget,
    RuntimeContext,
    discover_project_context,
)
from forge_agent.model import ModelClient, OpenAICompatibleClient
from forge_agent.safety import PolicyDecision, PolicyEngine, PolicyToolRuntime
from forge_agent.storage import SQLiteStorage
from forge_agent.tools import build_default_registry
from forge_agent.tools.git import collect_workspace_summary
from forge_agent.types import (
    AgentStatus,
    Message,
    RunResult,
    ToolCall,
    ToolResult,
    VerificationRecord,
)

ModelFactory = Callable[[RunConfig], ModelClient]
ApprovalHandler = Callable[[ToolCall, PolicyDecision], bool | Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class RunningSession:
    id: str
    task: asyncio.Task[RunResult]


class SessionService:
    def __init__(
        self,
        database_path: Path,
        *,
        events: EventBus | None = None,
        model_factory: ModelFactory | None = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> None:
        self.database_path = database_path
        self.events = events or EventBus()
        self.approvals = ApprovalBroker(self.events)
        self.model_factory = model_factory or self._default_model
        self.approval_handler = approval_handler
        self._running: dict[str, RunningSession] = {}

    def start_new(self, config: RunConfig, task: str) -> RunningSession:
        metadata = {
            "workspace": config.workspace.as_posix(),
            "task": task,
            "model": config.model,
            "mode": config.mode.value,
            "verify_command": config.verify_command,
            "status": AgentStatus.INITIALIZING.value,
        }
        with SQLiteStorage(self.database_path) as storage:
            session = storage.create_session(metadata=metadata)
        running = self._start(config, session.id, task, metadata, history=None)
        return running

    def resume(
        self,
        config: RunConfig,
        session_id: str,
        instruction: str,
    ) -> RunningSession:
        with SQLiteStorage(self.database_path) as storage:
            session = storage.get_session(session_id)
            if session is None:
                raise KeyError(f"unknown session: {session_id}")
            expected_workspace = Path(str(session.metadata.get("workspace", ""))).resolve()
            if expected_workspace != config.workspace:
                raise ValueError(
                    "the configured workspace differs from the persisted session workspace"
                )
            self._mark_interrupted_tools(storage, session_id)
            history = [record.message for record in storage.list_messages(session_id)]
            if not history:
                raise ValueError("session has no persisted conversation history")
            metadata = dict(session.metadata)
            metadata["mode"] = config.mode.value
            metadata["status"] = AgentStatus.INITIALIZING.value
            storage.update_session_metadata(session_id, metadata)
        return self._start(config, session_id, instruction, metadata, history=history)

    async def run_new(self, config: RunConfig, task: str) -> RunResult:
        return await self.start_new(config, task).task

    def cancel(self, session_id: str) -> bool:
        running = self._running.get(session_id)
        if running is None or running.task.done():
            return False
        self.approvals.reject_for_session(session_id)
        running.task.cancel()
        return True

    def running(self, session_id: str) -> RunningSession | None:
        return self._running.get(session_id)

    async def rollback_changes(
        self,
        config: RunConfig,
        session_id: str,
    ) -> ToolResult:
        if self.running(session_id) is not None:
            return ToolResult(
                ok=False,
                summary="cannot roll back while the session is running",
                error_code="session_running",
            )
        with SQLiteStorage(self.database_path) as storage:
            session = storage.get_session(session_id)
            if session is None:
                raise KeyError(f"unknown session: {session_id}")
            expected = Path(str(session.metadata.get("workspace", ""))).resolve()
            if expected != config.workspace:
                raise ValueError(
                    "the configured workspace differs from the persisted session workspace"
                )
            registry = build_default_registry(
                config.workspace,
                command_timeout_s=config.command_timeout_s,
                max_output_chars=config.max_tool_output_chars,
            )
            runtime = PersistentToolRuntime(
                PolicyToolRuntime(
                    registry,
                    PolicyEngine(mode=config.mode, auto_approve=True),
                ),
                storage,
                session_id,
                config.workspace,
            )
            call = ToolCall(
                id=f"manual-rollback-{session_id}",
                name="rollback_changes",
                arguments={},
            )
            started = {
                "call_id": call.id,
                "name": call.name,
                "arguments": {},
                "manual": True,
            }
            storage.append_event(session_id, "tool_started", started)
            self.events.publish(session_id, "tool_started", started)
            result = await runtime.execute(call)
            payload = {
                "call_id": call.id,
                "name": call.name,
                "ok": result.ok,
                "error_code": result.error_code,
                "duration_ms": result.duration_ms,
                "truncated": result.truncated,
                "summary": result.summary,
                "content": result.content,
                "metadata": result.metadata,
                "manual": True,
            }
            storage.append_event(session_id, "tool_finished", payload)
            self.events.publish(session_id, "tool_finished", payload)
            return result

    def _start(
        self,
        config: RunConfig,
        session_id: str,
        task: str,
        metadata: dict[str, Any],
        *,
        history: list[Message] | None,
    ) -> RunningSession:
        if session_id in self._running and not self._running[session_id].task.done():
            raise RuntimeError(f"session is already running: {session_id}")
        asyncio_task = asyncio.create_task(
            self._execute(config, session_id, task, metadata, history=history),
            name=f"forge-session-{session_id}",
        )
        running = RunningSession(session_id, asyncio_task)
        self._running[session_id] = running
        asyncio_task.add_done_callback(lambda _task: self._running.pop(session_id, None))
        return running

    async def _execute(
        self,
        config: RunConfig,
        session_id: str,
        task: str,
        metadata: dict[str, Any],
        *,
        history: list[Message] | None,
    ) -> RunResult:
        with SQLiteStorage(self.database_path) as storage:

            def on_event(kind: str, payload: dict[str, Any]) -> None:
                storage.append_event(session_id, kind, payload)
                self.events.publish(session_id, kind, payload)
                if kind == "context_compacted":
                    self._persist_compaction(storage, session_id, payload)
                if kind == "run_finished":
                    metadata["status"] = payload["status"]
                    storage.update_session_metadata(session_id, metadata)

            def on_message(message: Message) -> None:
                storage.append_message(session_id, message)

            async def approve(call: ToolCall, decision: PolicyDecision) -> bool:
                if self.approval_handler is None:
                    return await self.approvals.request(session_id, call, decision)
                approved = self.approval_handler(call, decision)
                if inspect.isawaitable(approved):
                    approved = await approved
                return approved

            model = self.model_factory(config)
            if isinstance(model, OpenAICompatibleClient):
                model.set_delta_callback(
                    lambda delta: self.events.publish(
                        session_id,
                        "model_delta",
                        {"text": delta},
                    )
                )
            discovered = discover_project_context(config.workspace)
            registry = build_default_registry(
                config.workspace,
                command_timeout_s=config.command_timeout_s,
                max_output_chars=config.max_tool_output_chars,
                suggested_verification=list(discovered.verification_commands),
            )
            policy_runtime = PolicyToolRuntime(
                registry,
                PolicyEngine(mode=config.mode, auto_approve=config.auto_approve),
                approve=approve,
            )
            tools = PersistentToolRuntime(
                policy_runtime,
                storage,
                session_id,
                config.workspace,
            )
            initial_summary, compacted_through = self._restore_compaction(
                storage, session_id
            )
            context = RuntimeContext(
                budget=ContextBudget(
                    context_window=config.context_window,
                    reserved_output_tokens=config.reserved_output_tokens,
                ),
                model=model,
                project_context=discovered.render(config.verify_command),
                max_tool_output_chars=config.max_tool_output_chars,
                initial_summary=initial_summary,
                compacted_through=compacted_through,
                on_event=on_event,
            )
            loop = AgentLoop(
                config=config,
                model=model,
                tools=tools,
                context=context,
                on_event=on_event,
                on_message=on_message,
                verification_commands=list(discovered.verification_commands),
            )
            policy_runtime.on_status = loop.state.set_status
            if history is not None:
                self._restore_state(loop, storage, session_id)
            try:
                result = await loop.run(task, history=history)
                result = await self._attach_workspace_summary(
                    result,
                    config,
                    on_event,
                )
            except asyncio.CancelledError:
                metadata["status"] = AgentStatus.CANCELLED.value
                storage.update_session_metadata(session_id, metadata)
                self.events.publish(
                    session_id,
                    "run_finished",
                    {"status": AgentStatus.CANCELLED.value, "summary": "Cancelled by user"},
                )
                self.approvals.clear_session(session_id)
                return RunResult(
                    status=AgentStatus.CANCELLED,
                    summary="Cancelled by user",
                    steps=loop.state.steps,
                    model_calls=loop.state.model_calls,
                    total_tokens=loop.state.total_tokens,
                    total_cost_usd=loop.state.total_cost_usd,
                    changed_files=sorted(loop.state.changed_files),
                    verification=loop.state.verification,
                )
            self._persist_evidence(storage, session_id, result)
            self.approvals.clear_session(session_id)
            return result

    @staticmethod
    async def _attach_workspace_summary(
        result: RunResult,
        config: RunConfig,
        on_event: Callable[[str, dict[str, Any]], None],
    ) -> RunResult:
        git_summary = await collect_workspace_summary(
            config.workspace,
            max_output_chars=config.max_tool_output_chars,
        )
        payload: dict[str, Any] = {
            "changed_files": result.changed_files,
            "verification_passed": (
                result.verification.passed if result.verification is not None else None
            ),
            "verification_command": (
                result.verification.command if result.verification is not None else None
            ),
            **git_summary,
        }
        on_event("workspace_summary", payload)
        return result.model_copy(update={"workspace_summary": payload})

    @staticmethod
    def _default_model(config: RunConfig) -> ModelClient:
        return OpenAICompatibleClient(
            api_key=config.api_key.get_secret_value(),
            model=config.model,
            base_url=config.base_url,
            stream=config.stream_model,
        )

    @staticmethod
    def _persist_evidence(
        storage: SQLiteStorage,
        session_id: str,
        result: RunResult,
    ) -> None:
        events = [
            (event.kind, event.payload)
            for event in storage.list_events(session_id)
        ]
        for claim in EvidenceLedger.from_run_result(result, events=events).claims:
            record = storage.save_claim(
                session_id,
                statement=claim.statement,
                status=claim.status.value,
            )
            for evidence in claim.evidence:
                storage.save_evidence(
                    record.id,
                    kind=evidence.kind,
                    description=evidence.description,
                    reference=evidence.reference,
                )

    @staticmethod
    def _persist_compaction(
        storage: SQLiteStorage,
        session_id: str,
        payload: dict[str, Any],
    ) -> None:
        records = storage.list_messages(session_id)
        through_index = int(payload.get("compacted_through", 0))
        summary = payload.get("summary")
        if (
            not isinstance(summary, dict)
            or through_index <= 0
            or through_index >= len(records)
        ):
            return
        through = records[through_index]
        retained = records[through_index + 1] if through_index + 1 < len(records) else None
        latest = storage.latest_compaction(session_id)
        if latest is None or latest.through_message_id != through.id:
            storage.save_compaction(
                session_id,
                through_message_id=through.id,
                retained_from_message_id=retained.id if retained else None,
                summary=summary,
            )

    @staticmethod
    def _restore_compaction(
        storage: SQLiteStorage,
        session_id: str,
    ) -> tuple[CompactionSummary | None, int]:
        latest = storage.latest_compaction(session_id)
        if latest is None:
            return None, 0
        compacted_through = 0
        for index, record in enumerate(storage.list_messages(session_id)):
            if record.id == latest.through_message_id:
                compacted_through = max(0, index)
                break
        return CompactionSummary.from_dict(latest.summary), compacted_through

    @staticmethod
    def _restore_state(
        loop: AgentLoop,
        storage: SQLiteStorage,
        session_id: str,
    ) -> None:
        for event in storage.list_events(session_id):
            if event.kind != "tool_finished":
                continue
            raw_metadata = event.payload.get("metadata")
            if not isinstance(raw_metadata, dict):
                continue
            changed = raw_metadata.get("changed_files")
            if isinstance(changed, list) and all(isinstance(path, str) for path in changed):
                loop.state.record_changes(changed)
            verification = raw_metadata.get("verification")
            if isinstance(verification, dict):
                try:
                    record = VerificationRecord.model_validate(
                        {
                            **verification,
                            "workspace_version": loop.state.workspace_version,
                        }
                    )
                except ValueError:
                    continue
                loop.state.record_verification(record)

    @staticmethod
    def _mark_interrupted_tools(
        storage: SQLiteStorage,
        session_id: str,
    ) -> None:
        pending: dict[str, dict[str, Any]] = {}
        for event in storage.list_events(session_id):
            call_id = event.payload.get("call_id")
            if not isinstance(call_id, str):
                continue
            if event.kind == "tool_started":
                pending[call_id] = event.payload
            elif event.kind in {"tool_finished", "tool_interrupted"}:
                pending.pop(call_id, None)
        for call_id, payload in pending.items():
            storage.append_event(
                session_id,
                "tool_interrupted",
                {
                    "call_id": call_id,
                    "name": payload.get("name", "unknown"),
                    "reason": "session resumed before a matching tool result was recorded",
                },
            )
