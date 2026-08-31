"""The explicit model-action-observation loop."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from forge_agent.agent.completion import CompletionJudge
from forge_agent.agent.plan_gate import uses_readonly_plan, wants_plan_first
from forge_agent.agent.prompts import (
    build_system_prompt,
    implement_approved_plan,
    verification_nudge,
)
from forge_agent.agent.state import AgentState
from forge_agent.config import RunConfig
from forge_agent.model.base import ContextOverflowError, ModelClient, ModelError
from forge_agent.safety.policy import (
    PARALLEL_READ_LIMIT,
    READ_ONLY_TOOLS,
    is_verification_command,
)
from forge_agent.types import (
    AgentStatus,
    Message,
    ModelResponse,
    RunMode,
    RunResult,
    ToolCall,
    ToolResult,
    VerificationRecord,
)


class ToolRuntime(Protocol):
    def schemas(self) -> list[dict[str, Any]]: ...

    async def execute(self, call: ToolCall) -> ToolResult: ...


class ContextRuntime(Protocol):
    async def prepare(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        force_compaction: bool = False,
    ) -> list[Message]: ...


EventCallback = Callable[[str, dict[str, Any]], None]
MessageCallback = Callable[[Message], None]
PlanApprovalCallback = Callable[[str], bool | Awaitable[bool]]
ModeChangeCallback = Callable[[RunMode], None]


def _interrupted_tool_message(call: ToolCall) -> Message:
    return Message(
        role="tool",
        tool_call_id=call.id,
        content=(
            f"{call.name} was interrupted before a result was recorded. "
            "Do not assume it succeeded."
        ),
    )


def repair_tool_history(messages: list[Message]) -> list[Message]:
    """Insert missing tool replies immediately after the assistant tool_calls."""

    repaired: list[Message] = []
    pending: list[ToolCall] = []
    answered: set[str] = set()

    def close_pending() -> None:
        nonlocal pending, answered
        for call in pending:
            if call.id not in answered:
                repaired.append(_interrupted_tool_message(call))
        pending = []
        answered = set()

    for message in messages:
        if message.role == "assistant":
            close_pending()
            repaired.append(message)
            pending = list(message.tool_calls)
            answered = set()
            continue
        if message.role == "tool":
            if message.tool_call_id and message.tool_call_id in {
                call.id for call in pending
            }:
                repaired.append(message)
                answered.add(message.tool_call_id)
            continue
        close_pending()
        repaired.append(message)
    close_pending()
    return repaired


class PassthroughContext:
    async def prepare(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        force_compaction: bool = False,
    ) -> list[Message]:
        del tools, force_compaction
        return list(messages)


class AgentLoop:
    def __init__(
        self,
        *,
        config: RunConfig,
        model: ModelClient,
        tools: ToolRuntime,
        context: ContextRuntime | None = None,
        on_event: EventCallback | None = None,
        on_message: MessageCallback | None = None,
        on_plan_approval: PlanApprovalCallback | None = None,
        on_mode_change: ModeChangeCallback | None = None,
        verification_commands: list[str] | None = None,
    ) -> None:
        self.config = config
        self.model = model
        self.tools = tools
        self.context = context or PassthroughContext()
        self.on_event = on_event or (lambda _event, _payload: None)
        self.on_message = on_message or (lambda _message: None)
        self.on_plan_approval = on_plan_approval
        self.on_mode_change = on_mode_change
        self.completion_judge = CompletionJudge()
        self.verification_commands = list(
            dict.fromkeys(
                [config.verify_command]
                if config.verify_command
                else (verification_commands or [])
            )
        )[:3]
        self._automatic_verification_version = -1
        self._plan_gate = False
        self._plan_resolved = False
        self.state = AgentState()
        self.messages: list[Message] = []

    async def run(
        self,
        task: str,
        *,
        history: list[Message] | None = None,
    ) -> RunResult:
        self.state.set_status(AgentStatus.INITIALIZING)
        self.state.begin_run()
        self._plan_gate = wants_plan_first(task, mode=self.config.mode)
        self._plan_resolved = False
        prompt_mode = (
            RunMode.PLAN
            if uses_readonly_plan(task, mode=self.config.mode)
            else self.config.mode
        )
        if history is None:
            self.messages = []
            self._append_message(
                Message(
                    role="system",
                    content=build_system_prompt(
                        self.config.workspace,
                        prompt_mode,
                        plan_then_build=self._plan_gate,
                    ),
                )
            )
            self._append_message(self._user_turn(task))
        else:
            self.messages = list(history)
            current_system = Message(
                role="system",
                content=build_system_prompt(
                    self.config.workspace,
                    prompt_mode,
                    plan_then_build=self._plan_gate,
                ),
            )
            if self.messages and self.messages[0].role == "system":
                self.messages[0] = current_system
            else:
                self.messages.insert(0, current_system)
            self.messages = repair_tool_history(self.messages)
            self._append_message(self._user_turn(task))
        self.on_event("run_started", {"task": task, "mode": self.config.mode.value})
        overflow_recovered = False

        try:
            while self.state.steps < self.config.max_steps:
                if self.state.model_calls >= self.config.max_model_calls:
                    return self._finish(AgentStatus.STOPPED, "Reached the model-call limit")

                self.state.steps += 1
                self.state.model_calls += 1
                self.state.set_status(AgentStatus.THINKING)
                schemas = self.tools.schemas()
                prepared = await self.context.prepare(self.messages, schemas)
                self.on_event(
                    "model_request",
                    {"step": self.state.steps, "message_count": len(prepared)},
                )

                try:
                    response = await self.model.complete(
                        prepared,
                        schemas,
                        timeout_s=self.config.model_timeout_s,
                    )
                except ContextOverflowError:
                    if overflow_recovered:
                        return self._finish(
                            AgentStatus.FAILED,
                            "Context overflow remained after one forced compaction",
                        )
                    overflow_recovered = True
                    self.state.model_calls -= 1
                    prepared = await self.context.prepare(
                        self.messages,
                        schemas,
                        force_compaction=True,
                    )
                    self.on_event("context_overflow_recovery", {"step": self.state.steps})
                    continue
                except ModelError as exc:
                    self.state.last_error = f"{exc.code}: {exc}"
                    return self._finish(AgentStatus.FAILED, self.state.last_error)

                self.state.total_tokens += response.usage.total_tokens
                call_cost = (
                    response.usage.input_tokens
                    * self.config.input_cost_per_million
                    + response.usage.output_tokens
                    * self.config.output_cost_per_million
                ) / 1_000_000
                self.state.total_cost_usd += call_cost
                self.on_event(
                    "model_response",
                    {
                        "step": self.state.steps,
                        "tool_calls": len(response.tool_calls),
                        "finish_reason": response.finish_reason,
                        "tokens": response.usage.total_tokens,
                        "total_tokens": self.state.total_tokens,
                        "max_total_tokens": self.config.max_total_tokens,
                        "max_steps": self.config.max_steps,
                        "call_cost_usd": call_cost,
                        "total_cost_usd": self.state.total_cost_usd,
                        "max_cost_usd": self.config.max_cost_usd,
                        "usage_estimated": response.usage.estimated,
                        "text": response.text or "",
                    },
                )
                if self.state.total_tokens >= self.config.max_total_tokens:
                    return self._finish(
                        AgentStatus.STOPPED,
                        f"Reached the token budget ({self.state.total_tokens} tokens)",
                    )
                if (
                    self.config.max_cost_usd is not None
                    and self.state.total_cost_usd >= self.config.max_cost_usd
                ):
                    return self._finish(
                        AgentStatus.STOPPED,
                        f"Reached the cost budget (${self.state.total_cost_usd:.4f})",
                    )
                overflow_recovered = False

                if response.tool_calls:
                    self.state.consecutive_empty_responses = 0
                    if self._needs_plan_approval():
                        deferred = await self._handle_plan_pass_calls(response)
                        if deferred is not None:
                            return deferred
                        continue
                    self._append_message(
                        Message(
                            role="assistant",
                            content=response.text,
                            tool_calls=response.tool_calls,
                        )
                    )
                    stopped = await self._execute_calls(response.tool_calls)
                    if stopped is not None:
                        return stopped
                    continue

                if response.text and response.text.strip():
                    self.state.consecutive_empty_responses = 0
                    if self._needs_plan_approval():
                        outcome = await self._offer_plan(response.text)
                        if outcome is not None:
                            return outcome
                        continue
                    decision = self.completion_judge.evaluate(self.state)
                    if (
                        not decision.accepted
                        and self.verification_commands
                        and self.state.run_changed_files
                        and self._automatic_verification_version
                        != self.state.workspace_version
                    ):
                        self._append_message(
                            Message(role="assistant", content=response.text)
                        )
                        self._automatic_verification_version = (
                            self.state.workspace_version
                        )
                        exhausted = await self._run_automatic_verification()
                        if exhausted:
                            return self._finish(
                                AgentStatus.STOPPED,
                                "The same failing verification was repeated without new evidence",
                            )
                        continue
                    if not decision.accepted and not self.state.completion_nudge_sent:
                        self.state.completion_nudge_sent = True
                        self._append_message(Message(role="assistant", content=response.text))
                        self._append_message(Message(role="user", content=verification_nudge()))
                        self.on_event("verification_required", {"step": self.state.steps})
                        continue
                    self._append_message(Message(role="assistant", content=response.text))
                    summary = response.text.strip()
                    if not decision.accepted:
                        summary += f"\n\nCompletion evidence: {decision.reason}"
                    return self._finish(decision.status, summary)

                self.state.consecutive_empty_responses += 1
                if self.state.consecutive_empty_responses >= 2:
                    return self._finish(
                        AgentStatus.FAILED,
                        "The model returned two consecutive empty responses",
                    )
                self._append_message(
                    Message(
                        role="user",
                        content=(
                            "The previous response was empty. Call an appropriate tool "
                            "or provide a final answer."
                        ),
                    )
                )

            return self._finish(AgentStatus.STOPPED, "Reached the maximum step count")
        except KeyboardInterrupt:
            return self._finish(AgentStatus.CANCELLED, "Cancelled by user")
        except Exception as exc:
            self.state.last_error = str(exc)
            self.on_event("unhandled_error", {"error": str(exc)})
            return self._finish(AgentStatus.FAILED, f"Unhandled error: {exc}")

    async def _execute_calls(self, calls: list[ToolCall]) -> RunResult | None:
        if self._can_run_in_parallel(calls):
            return await self._execute_parallel_readonly(calls)
        for call in calls:
            stopped = self._stop_if_repeated(call)
            if stopped is not None:
                return stopped
            self._emit_tool_started(call)
            result = await self.tools.execute(call)
            exhausted = self._record_tool_result(call, result)
            if exhausted:
                return self._finish(
                    AgentStatus.STOPPED,
                    "The same failing verification was repeated without new evidence",
                )
        return None

    async def _handle_plan_pass_calls(self, response: ModelResponse) -> RunResult | None:
        readonly = [call for call in response.tool_calls if call.name in READ_ONLY_TOOLS]
        premature = [call for call in response.tool_calls if call.name not in READ_ONLY_TOOLS]
        if readonly:
            self._append_message(
                Message(
                    role="assistant",
                    content=None if premature else response.text,
                    tool_calls=readonly,
                )
            )
            stopped = await self._execute_calls(readonly)
            if stopped is not None:
                return stopped
        plan_text = (response.text or "").strip()
        if premature and plan_text:
            return await self._offer_plan(plan_text)
        if premature and not plan_text:
            self._append_message(
                Message(
                    role="user",
                    content=(
                        "Do not edit yet. Output the plan as text only. "
                        "The user will confirm before any writes."
                    ),
                )
            )
        return None

    def _needs_plan_approval(self) -> bool:
        return self._plan_gate and not self._plan_resolved

    async def _offer_plan(self, plan_text: str) -> RunResult | None:
        self._append_message(Message(role="assistant", content=plan_text))
        self.state.set_status(AgentStatus.AWAITING_PLAN_APPROVAL)
        self.on_event(
            "plan_ready",
            {"plan": plan_text, "step": self.state.steps, "mode": self.config.mode.value},
        )
        approved = True
        if not self.config.auto_approve:
            if self.on_plan_approval is None:
                return self._finish(AgentStatus.COMPLETED, plan_text.strip())
            result = self.on_plan_approval(plan_text)
            if inspect.isawaitable(result):
                approved = await result
            else:
                approved = bool(result)
        if not approved:
            return self._finish(AgentStatus.STOPPED, "The user declined to execute the plan")
        self._enter_build_after_plan()
        return None

    def _enter_build_after_plan(self) -> None:
        self._plan_resolved = True
        self.config.mode = RunMode.BUILD
        if self.messages and self.messages[0].role == "system":
            self.messages[0] = Message(
                role="system",
                content=build_system_prompt(self.config.workspace, RunMode.BUILD),
            )
        if self.on_mode_change is not None:
            self.on_mode_change(RunMode.BUILD)
        self.on_event("plan_approved", {"mode": RunMode.BUILD.value})
        self._append_message(Message(role="user", content=implement_approved_plan()))
        self.state.set_status(AgentStatus.THINKING)

    async def _execute_parallel_readonly(self, calls: list[ToolCall]) -> RunResult | None:
        to_run: list[ToolCall] = []
        repeated: ToolCall | None = None
        for call in calls:
            if self.state.record_tool_call(call) >= 3:
                repeated = call
                break
            to_run.append(call)
        if to_run:
            self.state.set_status(AgentStatus.EXECUTING_TOOL)
            for call in to_run:
                self._emit_tool_started(call)
            limit = asyncio.Semaphore(PARALLEL_READ_LIMIT)

            async def run(call: ToolCall) -> ToolResult:
                async with limit:
                    return await self.tools.execute(call)

            results = await asyncio.gather(*(run(call) for call in to_run))
            for call, result in zip(to_run, results, strict=True):
                exhausted = self._record_tool_result(call, result)
                if exhausted:
                    return self._finish(
                        AgentStatus.STOPPED,
                        "The same failing verification was repeated without new evidence",
                    )
        if repeated is not None:
            return self._finish(
                AgentStatus.STOPPED,
                f"Repeated the same action three times: {repeated.name}",
            )
        return None

    def _can_run_in_parallel(self, calls: list[ToolCall]) -> bool:
        return len(calls) >= 2 and all(call.name in READ_ONLY_TOOLS for call in calls)

    def _stop_if_repeated(self, call: ToolCall) -> RunResult | None:
        if self.state.record_tool_call(call) >= 3:
            return self._finish(
                AgentStatus.STOPPED,
                f"Repeated the same action three times: {call.name}",
            )
        return None

    def _emit_tool_started(self, call: ToolCall) -> None:
        self.state.set_status(AgentStatus.EXECUTING_TOOL)
        self.on_event(
            "tool_started",
            {"call_id": call.id, "name": call.name, "arguments": call.arguments},
        )

    def _record_tool_result(self, call: ToolCall, result: ToolResult) -> bool:
        exhausted = self._apply_tool_metadata(call, result)
        self._append_message(
            Message(
                role="tool",
                tool_call_id=call.id,
                content=result.as_observation(),
            )
        )
        self.on_event(
            "tool_finished",
            {
                "name": call.name,
                "call_id": call.id,
                "ok": result.ok,
                "error_code": result.error_code,
                "duration_ms": result.duration_ms,
                "truncated": result.truncated,
                "summary": result.summary,
                "content": result.content,
                "metadata": result.metadata,
                "arguments": call.arguments,
            },
        )
        return exhausted

    async def _run_automatic_verification(self) -> bool:
        exhausted = False
        self.on_event(
            "automatic_verification_started",
            {
                "commands": self.verification_commands,
                "workspace_version": self.state.workspace_version,
            },
        )
        for index, command in enumerate(self.verification_commands):
            call = ToolCall(
                id=f"automatic-verification-{self.state.workspace_version}-{index}",
                name="verify_changes",
                arguments={"command": command},
            )
            self.state.set_status(AgentStatus.VERIFYING)
            self.on_event(
                "tool_started",
                {
                    "name": call.name,
                    "call_id": call.id,
                    "arguments": call.arguments,
                    "automatic": True,
                },
            )
            result = await self.tools.execute(call)
            exhausted = self._apply_tool_metadata(call, result)
            self._append_message(
                Message(
                    role="user",
                    content=(
                        "[Automatic verification result]\n"
                        f"Command: {command}\n{result.as_observation()}"
                    ),
                )
            )
            self.on_event(
                "tool_finished",
                {
                    "name": call.name,
                    "call_id": call.id,
                    "ok": result.ok,
                    "error_code": result.error_code,
                    "duration_ms": result.duration_ms,
                    "truncated": result.truncated,
                    "summary": result.summary,
                    "content": result.content,
                    "metadata": result.metadata,
                    "automatic": True,
                },
            )
            if exhausted or not result.ok:
                break
        self.on_event(
            "automatic_verification_finished",
            {
                "passed": bool(
                    self.state.verification and self.state.verification.passed
                ),
                "workspace_version": self.state.workspace_version,
            },
        )
        return exhausted

    def _user_turn(self, task: str) -> Message:
        return Message(
            role="user",
            content=task,
            attachments=list(self.config.user_image_data_urls),
        )

    def _append_message(self, message: Message) -> None:
        self.messages.append(message)
        self.on_message(message)

    def _apply_tool_metadata(self, call: ToolCall, result: ToolResult) -> bool:
        changed = result.metadata.get("changed_files", [])
        if isinstance(changed, list) and all(isinstance(item, str) for item in changed):
            self.state.record_changes(changed)

        verification = result.metadata.get("verification")
        record: VerificationRecord | None = None
        if isinstance(verification, dict):
            try:
                record = VerificationRecord.model_validate(
                    {
                        **verification,
                        "workspace_version": self.state.workspace_version,
                        "duration_ms": result.duration_ms,
                        "output": result.content,
                    }
                )
            except ValueError:
                return False
        elif (
            call.name == "verify_changes"
            or result.metadata.get("is_verification") is True
            or (
                call.name == "run_command"
                and is_verification_command(
                    str(call.arguments.get("command") or result.metadata.get("command") or "")
                )
            )
        ):
            command = str(result.metadata.get("command", call.arguments.get("command", "")))
            exit_code = int(result.metadata.get("exit_code", 0 if result.ok else 1))
            record = VerificationRecord(
                command=command,
                exit_code=exit_code,
                workspace_version=self.state.workspace_version,
                duration_ms=result.duration_ms,
                output=result.content,
            )
        if record is None:
            return False
        self.state.record_verification(record)
        self.state.set_status(AgentStatus.VERIFYING)
        if record.passed:
            return False
        hypothesis = self.state.record_failed_verification(record)
        self.on_event(
            "hypothesis_updated",
            {
                "signature": hypothesis.signature,
                "observed_failure": hypothesis.observed_failure,
                "hypothesis": hypothesis.hypothesis,
                "experiments": hypothesis.experiments,
                "retired": hypothesis.retired,
                "last_result": hypothesis.last_result,
            },
        )
        return hypothesis.retired

    def _finish(self, status: AgentStatus, summary: str) -> RunResult:
        self.state.set_status(status)
        self.on_event(
            "run_finished",
            {"status": status.value, "steps": self.state.steps, "summary": summary},
        )
        return RunResult(
            status=status,
            summary=summary,
            steps=self.state.steps,
            model_calls=self.state.model_calls,
            total_tokens=self.state.total_tokens,
            total_cost_usd=self.state.total_cost_usd,
            changed_files=sorted(self.state.run_changed_files),
            verification=self.state.verification,
        )
