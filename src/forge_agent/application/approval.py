"""Non-blocking human approval broker."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from forge_agent.application.events import EventBus
from forge_agent.safety import PolicyDecision
from forge_agent.types import ToolCall


@dataclass(frozen=True, slots=True)
class PendingApproval:
    id: str
    session_id: str
    call: ToolCall
    decision: PolicyDecision


class ApprovalBroker:
    def __init__(self, events: EventBus) -> None:
        self.events = events
        self._pending: dict[str, tuple[PendingApproval, asyncio.Future[bool]]] = {}
        self._session_grants: set[tuple[str, str]] = set()

    async def request(
        self,
        session_id: str,
        call: ToolCall,
        decision: PolicyDecision,
    ) -> bool:
        if (session_id, call.name) in self._session_grants:
            self.events.publish(
                session_id,
                "approval_reused",
                {"tool": call.name, "scope": "session"},
            )
            return True
        request_id = uuid.uuid4().hex
        approval = PendingApproval(request_id, session_id, call, decision)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (approval, future)
        self.events.publish(
            session_id,
            "approval_requested",
            {
                "approval_id": request_id,
                "tool": call.name,
                "arguments": call.arguments,
                "risk": decision.risk.value,
                "reason": decision.reason,
            },
        )
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    def resolve(
        self,
        approval_id: str,
        approved: bool,
        *,
        remember_for_session: bool = False,
    ) -> bool:
        item = self._pending.get(approval_id)
        if item is None:
            return False
        approval, future = item
        if future.done():
            return False
        if approved and remember_for_session:
            self._session_grants.add((approval.session_id, approval.call.name))
        future.set_result(approved)
        self.events.publish(
            approval.session_id,
            "approval_resolved",
            {
                "approval_id": approval_id,
                "approved": approved,
                "scope": "session" if remember_for_session else "once",
            },
        )
        return True

    def pending(self, session_id: str | None = None) -> list[PendingApproval]:
        values = [item[0] for item in self._pending.values()]
        if session_id is None:
            return values
        return [item for item in values if item.session_id == session_id]

    def reject_for_session(self, session_id: str) -> None:
        for approval in self.pending(session_id):
            self.resolve(approval.id, False)

    def clear_session(self, session_id: str) -> None:
        self.reject_for_session(session_id)
        self._session_grants = {
            grant for grant in self._session_grants if grant[0] != session_id
        }
