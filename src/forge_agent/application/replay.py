"""Timing helpers for no-side-effect event replay."""

from __future__ import annotations

from datetime import datetime


def replay_delay_seconds(
    previous_created_at: str | None,
    current_created_at: str,
    speed: float,
    *,
    max_delay_s: float = 5.0,
) -> float:
    """Return how long to wait before showing the next historical event."""

    if speed <= 0 or previous_created_at is None:
        return 0.0
    try:
        previous = datetime.fromisoformat(previous_created_at.replace("Z", "+00:00"))
        current = datetime.fromisoformat(current_created_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    delay = (current - previous).total_seconds() / speed
    if delay <= 0:
        return 0.0
    return min(delay, max_delay_s)
