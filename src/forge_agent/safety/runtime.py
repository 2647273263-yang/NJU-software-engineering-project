"""Policy-enforced tool runtime."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from forge_agent.safety.policy import PolicyDecision, PolicyEngine
from forge_agent.tools.registry import ToolRegistry
from forge_agent.types import AgentStatus, ToolCall, ToolResult

ApprovalCallback = Callable[[ToolCall, PolicyDecision], bool | Awaitable[bool]]
StatusCallback = Callable[[AgentStatus], None]


class PolicyToolRuntime:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: PolicyEngine,
        *,
        approve: ApprovalCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.approve = approve
        self.on_status = on_status

    def schemas(self) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for spec in self.registry:
            decision = self.policy.evaluate(spec.name, {})
            if decision.allowed:
                schemas.append(spec.json_schema())
        return schemas

    async def execute(self, call: ToolCall) -> ToolResult:
        decision = self.policy.evaluate(call.name, call.arguments)
        if not decision.allowed:
            return ToolResult(
                ok=False,
                summary=f"Policy denied {call.name}: {decision.reason}",
                error_code="policy_denied",
                metadata={"risk": decision.risk.value},
            )
        if decision.requires_approval:
            if self.on_status is not None:
                self.on_status(AgentStatus.AWAITING_APPROVAL)
            if self.approve is None:
                return ToolResult(
                    ok=False,
                    summary=f"Approval required for {call.name}: {decision.reason}",
                    error_code="approval_required",
                    metadata={"risk": decision.risk.value},
                )
            approved = self.approve(call, decision)
            if inspect.isawaitable(approved):
                approved = await approved
            if not approved:
                return ToolResult(
                    ok=False,
                    summary=f"User rejected {call.name}: {decision.reason}",
                    error_code="user_rejected",
                    metadata={"risk": decision.risk.value},
                )
            if self.on_status is not None:
                self.on_status(AgentStatus.EXECUTING_TOOL)
        return await self.registry.execute(call)
