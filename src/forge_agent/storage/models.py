"""Immutable records returned by the persistence layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forge_agent.types import Message


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: int
    session_id: str
    sequence: int
    message: Message
    created_at: str


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: int
    session_id: str
    kind: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class CompactionRecord:
    id: int
    session_id: str
    through_message_id: int
    retained_from_message_id: int | None
    summary: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class EditTransactionRecord:
    id: int
    session_id: str
    status: str
    metadata: dict[str, Any]
    created_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    id: int
    edit_transaction_id: int
    path: str
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    id: int
    session_id: str
    edit_transaction_id: int | None
    statement: str
    status: str
    created_at: str


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    id: int
    claim_id: int
    kind: str
    description: str
    reference: str | None
    metadata: dict[str, Any]
    created_at: str
