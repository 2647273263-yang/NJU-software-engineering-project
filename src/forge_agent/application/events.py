"""In-process event stream shared by CLI and GUI frontends."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    session_id: str
    kind: str
    payload: dict[str, Any]
    created_at: str


class EventBus:
    """Fan out runtime events to independent bounded subscribers."""

    def __init__(self, *, queue_size: int = 1_000) -> None:
        self.queue_size = queue_size
        self._subscribers: set[asyncio.Queue[ApplicationEvent]] = set()

    def subscribe(self) -> asyncio.Queue[ApplicationEvent]:
        queue: asyncio.Queue[ApplicationEvent] = asyncio.Queue(self.queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[ApplicationEvent]) -> None:
        self._subscribers.discard(queue)

    def publish(self, session_id: str, kind: str, payload: dict[str, Any]) -> None:
        event = ApplicationEvent(
            session_id=session_id,
            kind=kind,
            payload=payload,
            created_at=datetime.now(UTC).isoformat(),
        )
        stale: list[asyncio.Queue[ApplicationEvent]] = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                stale.append(queue)
        for queue in stale:
            self._subscribers.discard(queue)
