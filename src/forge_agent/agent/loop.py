"""The explicit model-action-observation loop."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from forge_agent.agent.completion import CompletionJudge
from forge_agent.agent.prompts import build_system_prompt, verification_nudge
from forge_agent.agent.state import AgentState
from forge_agent.config import RunConfig
from forge_agent.model.base import ContextOverflowError, ModelClient, ModelError
from forge_agent.types import (
    AgentStatus,
    Message,
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
        verification_commands: list[str] | None = None,
    ) -> None:
        self.config = config
        self.model = model
        self.tools = tools
        self.context = context or PassthroughContext()
        self.on_event = on_event or (lambda _event, _payload: None)
        self.on_message = on_message or (lambda _message: None)
        self.completion_judge = CompletionJudge()
        self.verification_commands = list(
            dict.fromkeys(
                [config.verify_command]
                if config.verify_command
                else (verification_commands or [])
            )
        )[:3]
        self._automatic_verification_version = -1
        self.state = AgentState()
        self.messages: list[Message] = []

    async def run(
        self,
        task: str,
        *,
        history: list[Message] | None = None,
    ) -> RunResult:
        self.state.set_status(AgentStatus.INITIALIZING)
        if history is None:
            self.messages = []
            self._append_message(
                Message(
                    role="system",
                    content=build_system_prompt(self.config.workspace, self.config.mode),
                )
            )
            self._append_message(Message(role="user", content=task))
        else:
            self.messages = list(history)
            current_system = Message(
                role="system",
                content=build_system_prompt(self.config.workspace, self.config.mode),
            )
            if self.messages and self.messages[0].role == "system":
                self.messages[0] = current_system
            else:
                self.messages.insert(0, current_system)
            self._append_message(Message(role="user", content=task))
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
                    decision = self.completion_judge.evaluate(self.state)
                    if (
                        not decision.accepted
                        and self.verification_commands
                        and self.state.changed_files
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
        for call in calls:
            if self.state.record_tool_call(call) >= 3:
                return self._finish(
                    AgentStatus.STOPPED,
                    f"Repeated the same action three times: {call.name}",
                )
            self.state.set_status(AgentStatus.EXECUTING_TOOL)
            self.on_event(
                "tool_started",
                {"call_id": call.id, "name": call.name, "arguments": call.arguments},
            )
            result = await self.tools.execute(call)
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
            if exhausted:
                return self._finish(
                    AgentStatus.STOPPED,
                    "The same failing verification was repeated without new evidence",
                )
        return None

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
        elif call.name == "verify_changes" or result.metadata.get("is_verification") is True:
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
            changed_files=sorted(self.state.changed_files),
            verification=self.state.verification,
        )
