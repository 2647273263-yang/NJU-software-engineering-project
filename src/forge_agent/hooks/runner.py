"""Run workspace hooks. Default path is type:prompt; command hooks stay off."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge_agent.agent.llm_judge import (
    JUDGE_SYSTEM,
    JudgeVerdict,
    build_judge_user_payload,
    judge_messages,
    parse_judge_response,
)
from forge_agent.agent.state import AgentState
from forge_agent.hooks.dangerous import (
    dangerous_command_reason,
    match_dangerous_command,
    tool_carries_shell,
)
from forge_agent.hooks.exfil import match_secret_or_escape_command, secret_shell_reason
from forge_agent.hooks.spec import (
    BUILTIN_DANGEROUS_BASH,
    BUILTIN_SECRET_SHELL,
    HookConfig,
    HookSpec,
    HookType,
    before_tool_hooks,
    load_hook_config,
    stop_hooks,
)
from forge_agent.model.base import ModelClient, ModelError
from forge_agent.types import TokenUsage, ToolCall, ToolResult, VerificationRecord

EventCallback = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class StopHookDecision:
    allow_stop: bool
    reason: str
    missing: list[str] = field(default_factory=list)
    hook_id: str = ""
    hook_type: str = HookType.PROMPT.value
    skipped: bool = False
    parse_error: bool = False
    usage: TokenUsage = field(default_factory=TokenUsage)
    verdict: JudgeVerdict | None = None

    def inject_message(self) -> str:
        if self.verdict is not None:
            return self.verdict.inject_message()
        fallback = JudgeVerdict(
            accepted=False,
            reason=self.reason,
            missing=self.missing,
            parse_error=self.parse_error,
        )
        return fallback.inject_message()


@dataclass(frozen=True, slots=True)
class BeforeToolDecision:
    allowed: bool
    reason: str = ""
    hook_id: str = ""
    pattern: str | None = None

    def as_result(self) -> ToolResult:
        return ToolResult(
            ok=False,
            summary=self.reason or "before_tool hook denied this call",
            error_code="hook_denied",
            metadata={
                "hook_id": self.hook_id,
                "event": "before_tool",
                "pattern": self.pattern,
            },
        )


class HookRunner:
    def __init__(
        self,
        *,
        config: HookConfig,
        model: ModelClient,
        on_event: EventCallback | None = None,
        workspace: Path | None = None,
    ) -> None:
        self.config = config
        self.model = model
        self.on_event = on_event or (lambda _kind, _payload: None)
        self.workspace = workspace

    @classmethod
    def load(
        cls,
        workspace: Path,
        model: ModelClient,
        *,
        on_event: EventCallback | None = None,
    ) -> HookRunner:
        return cls(
            config=load_hook_config(workspace),
            model=model,
            on_event=on_event,
            workspace=workspace,
        )

    def before_tool(self, call: ToolCall) -> BeforeToolDecision:
        """Deny destructive or secret-exfiltrating shell before Policy asks for approval."""

        for spec in before_tool_hooks(self.config):
            if spec.type is HookType.COMMAND and not self.config.allow_command_hooks:
                self.on_event(
                    "hook_skipped",
                    {
                        "hook_id": spec.id,
                        "event": spec.event.value,
                        "type": spec.type.value,
                        "reason": "command hooks are disabled",
                    },
                )
                continue
            if spec.id in {BUILTIN_DANGEROUS_BASH.id, "block_dangerous_bash"}:
                decision = self._dangerous_bash_decision(call)
                if not decision.allowed:
                    return decision
                continue
            if spec.id in {BUILTIN_SECRET_SHELL.id, "block_secret_shell"}:
                decision = self._secret_shell_decision(call)
                if not decision.allowed:
                    return decision
                continue
        return BeforeToolDecision(allowed=True)

    def _dangerous_bash_decision(self, call: ToolCall) -> BeforeToolDecision:
        if not tool_carries_shell(call.name):
            return BeforeToolDecision(allowed=True, hook_id=BUILTIN_DANGEROUS_BASH.id)
        command = str(call.arguments.get("command") or "")
        pattern = match_dangerous_command(command)
        if pattern is None:
            return BeforeToolDecision(allowed=True, hook_id=BUILTIN_DANGEROUS_BASH.id)
        return self._deny_shell(
            call,
            hook_id=BUILTIN_DANGEROUS_BASH.id,
            pattern=pattern,
            reason=dangerous_command_reason(pattern),
        )

    def _secret_shell_decision(self, call: ToolCall) -> BeforeToolDecision:
        if not tool_carries_shell(call.name):
            return BeforeToolDecision(allowed=True, hook_id=BUILTIN_SECRET_SHELL.id)
        command = str(call.arguments.get("command") or "")
        pattern = match_secret_or_escape_command(command, self.workspace)
        if pattern is None:
            return BeforeToolDecision(allowed=True, hook_id=BUILTIN_SECRET_SHELL.id)
        return self._deny_shell(
            call,
            hook_id=BUILTIN_SECRET_SHELL.id,
            pattern=pattern,
            reason=secret_shell_reason(pattern),
        )

    def _deny_shell(
        self,
        call: ToolCall,
        *,
        hook_id: str,
        pattern: str,
        reason: str,
    ) -> BeforeToolDecision:
        self.on_event(
            "hook_denied",
            {
                "hook_id": hook_id,
                "event": "before_tool",
                "type": "prompt",
                "name": call.name,
                "call_id": call.id,
                "arguments": call.arguments,
                "reason": reason,
                "pattern": pattern,
            },
        )
        return BeforeToolDecision(
            allowed=False,
            reason=reason,
            hook_id=hook_id,
            pattern=pattern,
        )

    async def on_stop_attempted(
        self,
        *,
        task: str,
        last_reply: str,
        state: AgentState,
        evidence_lines: list[str],
        timeout_s: float,
    ) -> StopHookDecision:
        hooks = [
            spec
            for spec in stop_hooks(self.config)
            if spec.type is HookType.PROMPT or self.config.allow_command_hooks
        ]
        if not hooks:
            return StopHookDecision(
                allow_stop=True,
                reason="No stop hooks are enabled.",
                skipped=True,
            )
        payload = build_judge_user_payload(
            task=task,
            last_reply=last_reply,
            changed_files=sorted(state.run_changed_files),
            verification=state.verification,
            evidence_lines=evidence_lines,
        )
        blocked: StopHookDecision | None = None
        usage = TokenUsage()
        for spec in hooks:
            if spec.type is HookType.COMMAND:
                self.on_event(
                    "hook_skipped",
                    {
                        "hook_id": spec.id,
                        "event": spec.event.value,
                        "type": spec.type.value,
                        "reason": "command hooks are disabled",
                    },
                )
                continue
            decision = await self._run_prompt_stop(
                spec,
                payload=payload,
                timeout_s=min(spec.timeout_s, timeout_s),
            )
            usage = TokenUsage(
                input_tokens=usage.input_tokens + decision.usage.input_tokens,
                output_tokens=usage.output_tokens + decision.usage.output_tokens,
                estimated=usage.estimated or decision.usage.estimated,
            )
            if not decision.allow_stop:
                blocked = decision
                break
        if blocked is not None:
            return StopHookDecision(
                allow_stop=False,
                reason=blocked.reason,
                missing=blocked.missing,
                hook_id=blocked.hook_id,
                parse_error=blocked.parse_error,
                usage=usage,
                verdict=blocked.verdict,
            )
        return StopHookDecision(
            allow_stop=True,
            reason="All prompt stop hooks accepted.",
            hook_id=hooks[0].id if hooks else "",
            usage=usage,
        )

    async def _run_prompt_stop(
        self,
        spec: HookSpec,
        *,
        payload: str,
        timeout_s: float,
    ) -> StopHookDecision:
        system_prompt = spec.prompt or JUDGE_SYSTEM
        messages = judge_messages(system_prompt=system_prompt, payload=payload)
        stream = getattr(self.model, "_stream", None)
        delta = getattr(self.model, "_on_delta", None)
        if stream is not None:
            self.model._stream = False
        if hasattr(self.model, "set_delta_callback"):
            self.model.set_delta_callback(lambda _delta: None)
        try:
            response = await self.model.complete(messages, [], timeout_s=timeout_s)
        except ModelError as exc:
            self.on_event(
                "hook_failed",
                {
                    "hook_id": spec.id,
                    "event": spec.event.value,
                    "type": spec.type.value,
                    "error": f"{exc.code}: {exc}"[:300],
                },
            )
            verdict = JudgeVerdict(
                accepted=False,
                reason="The inspector call failed.",
                missing=["Re-check the original task; the inspector could not finish."],
            )
            return StopHookDecision(
                allow_stop=False,
                reason=verdict.reason,
                missing=verdict.missing,
                hook_id=spec.id,
                verdict=verdict,
            )
        except Exception as exc:
            self.on_event(
                "hook_failed",
                {
                    "hook_id": spec.id,
                    "event": spec.event.value,
                    "type": spec.type.value,
                    "error": str(exc)[:300],
                },
            )
            verdict = JudgeVerdict(
                accepted=False,
                reason="The inspector call failed.",
                missing=["Re-check the original task; the inspector could not finish."],
            )
            return StopHookDecision(
                allow_stop=False,
                reason=verdict.reason,
                missing=verdict.missing,
                hook_id=spec.id,
                verdict=verdict,
            )
        finally:
            if stream is not None:
                self.model._stream = stream
            if hasattr(self.model, "set_delta_callback") and delta is not None:
                self.model.set_delta_callback(delta)

        verdict = parse_judge_response(response.text or "")
        if verdict.parse_error:
            self.on_event(
                "hook_failed",
                {
                    "hook_id": spec.id,
                    "event": spec.event.value,
                    "type": spec.type.value,
                    "error": "unparsable prompt hook output",
                },
            )
        return StopHookDecision(
            allow_stop=verdict.accepted,
            reason=verdict.reason,
            missing=verdict.missing,
            hook_id=spec.id,
            parse_error=verdict.parse_error,
            usage=response.usage,
            verdict=verdict,
        )


def evidence_lines_for_judge(
    *,
    changed_files: list[str],
    verification: VerificationRecord | None,
) -> list[str]:
    lines = [f"changed:{path}" for path in changed_files]
    if verification is not None:
        lines.append(
            f"verification:{verification.command} exit={verification.exit_code} "
            f"passed={verification.passed}"
        )
    return lines


__all__ = [
    "BeforeToolDecision",
    "HookRunner",
    "StopHookDecision",
    "evidence_lines_for_judge",
]
