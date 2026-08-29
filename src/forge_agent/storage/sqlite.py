"""SQLite persistence with explicit transactions and JSON serialization."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from forge_agent.storage.models import (
    ClaimRecord,
    CompactionRecord,
    EditTransactionRecord,
    EventRecord,
    EvidenceRecord,
    MessageRecord,
    SessionRecord,
    SnapshotRecord,
)
from forge_agent.types import Message

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content TEXT,
    tool_call_id TEXT,
    tool_calls_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(session_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_messages_session_sequence
    ON messages(session_id, sequence);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session_id, id);
CREATE TABLE IF NOT EXISTS compactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    through_message_id INTEGER NOT NULL REFERENCES messages(id),
    retained_from_message_id INTEGER REFERENCES messages(id),
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_compactions_session_id ON compactions(session_id, id);
CREATE TABLE IF NOT EXISTS edit_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'open',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_edit_transactions_session_id
    ON edit_transactions(session_id, id);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edit_transaction_id INTEGER NOT NULL
        REFERENCES edit_transactions(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_snapshots_transaction_id
    ON snapshots(edit_transaction_id, id);
CREATE INDEX IF NOT EXISTS idx_snapshots_transaction_path
    ON snapshots(edit_transaction_id, path);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    edit_transaction_id INTEGER REFERENCES edit_transactions(id) ON DELETE SET NULL,
    statement TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_claims_session_id ON claims(session_id, id);
CREATE INDEX IF NOT EXISTS idx_claims_transaction_id
    ON claims(edit_transaction_id, id);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    reference TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_evidence_claim_id ON evidence(claim_id, id);
"""
_SCHEMA_VERSION = 1


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class SQLiteStorage:
    """Small synchronous store owning one SQLite connection.

    The instance is intended for one thread. Calls made inside ``transaction()``
    participate in that transaction; nested transactions use savepoints.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        current_version = int(
            self._connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if current_version > _SCHEMA_VERSION:
            self._connection.close()
            raise RuntimeError(
                f"database schema {current_version} is newer than supported "
                f"version {_SCHEMA_VERSION}"
            )
        self._connection.executescript(_SCHEMA)
        if current_version < _SCHEMA_VERSION:
            self._connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        self._transaction_depth = 0

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteStorage:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        depth = self._transaction_depth
        savepoint = f"forge_savepoint_{depth}"
        if depth == 0:
            self._connection.execute("BEGIN IMMEDIATE")
        else:
            self._connection.execute(f"SAVEPOINT {savepoint}")
        self._transaction_depth += 1
        try:
            yield self._connection
        except BaseException:
            self._transaction_depth -= 1
            if depth == 0:
                self._connection.rollback()
            else:
                self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            self._transaction_depth -= 1
            if depth == 0:
                self._connection.commit()
            else:
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")

    def create_session(
        self,
        session_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SessionRecord:
        identifier = session_id or uuid.uuid4().hex
        self._connection.execute(
            "INSERT INTO sessions(id, metadata_json) VALUES (?, ?)",
            (identifier, _json(dict(metadata or {}))),
        )
        record = self.get_session(identifier)
        assert record is not None
        return record

    def get_session(self, session_id: str) -> SessionRecord | None:
        row = self._connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return SessionRecord(
            id=row["id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=json.loads(row["metadata_json"]),
        )

    def update_session_metadata(
        self, session_id: str, metadata: Mapping[str, Any]
    ) -> SessionRecord:
        cursor = self._connection.execute(
            """
            UPDATE sessions
            SET metadata_json = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (_json(dict(metadata)), session_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown session: {session_id}")
        record = self.get_session(session_id)
        assert record is not None
        return record

    def patch_session_metadata(
        self, session_id: str, fields: Mapping[str, Any]
    ) -> SessionRecord:
        """Merge fields into session metadata without replacing the whole JSON object."""

        if not fields:
            record = self.get_session(session_id)
            if record is None:
                raise KeyError(f"unknown session: {session_id}")
            return record
        expr = "metadata_json"
        params: list[Any] = []
        for key, value in fields.items():
            if not str(key).isidentifier():
                raise ValueError(f"invalid metadata key: {key}")
            expr = f"json_set({expr}, ?, json(?))"
            params.append(f"$.{key}")
            params.append(json.dumps(value, ensure_ascii=False))
        params.append(session_id)
        cursor = self._connection.execute(
            f"""
            UPDATE sessions
            SET metadata_json = {expr},
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            params,
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown session: {session_id}")
        record = self.get_session(session_id)
        assert record is not None
        return record

    def delete_session(self, session_id: str) -> None:
        cursor = self._connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        if cursor.rowcount != 1:
            raise KeyError(f"unknown session: {session_id}")

    def append_message(self, session_id: str, message: Message) -> MessageRecord:
        with self.transaction():
            sequence = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            cursor = self._connection.execute(
                """
                INSERT INTO messages(
                    session_id, sequence, role, content, tool_call_id, tool_calls_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    sequence,
                    message.role,
                    message.content,
                    message.tool_call_id,
                    _json([call.model_dump(mode="json") for call in message.tool_calls]),
                ),
            )
            self._touch(session_id)
        message_id = cursor.lastrowid
        assert message_id is not None
        return self.get_message(message_id)

    def replace_messages(self, session_id: str, messages: list[Message]) -> None:
        with self.transaction():
            self._connection.execute(
                "DELETE FROM compactions WHERE session_id = ?", (session_id,)
            )
            self._connection.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            for sequence, message in enumerate(messages, start=1):
                self._connection.execute(
                    """
                    INSERT INTO messages(
                        session_id, sequence, role, content, tool_call_id, tool_calls_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        sequence,
                        message.role,
                        message.content,
                        message.tool_call_id,
                        _json([call.model_dump(mode="json") for call in message.tool_calls]),
                    ),
                )
            self._touch(session_id)

    def get_message(self, message_id: int) -> MessageRecord:
        row = self._connection.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown message: {message_id}")
        return self._message_record(row)

    def list_messages(
        self, session_id: str, *, after_id: int | None = None
    ) -> list[MessageRecord]:
        if after_id is None:
            rows = self._connection.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            )
        else:
            rows = self._connection.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ? AND id > ?
                ORDER BY sequence
                """,
                (session_id, after_id),
            )
        return [self._message_record(row) for row in rows]

    def append_event(
        self, session_id: str, kind: str, payload: Mapping[str, Any]
    ) -> EventRecord:
        with self.transaction():
            cursor = self._connection.execute(
                "INSERT INTO events(session_id, kind, payload_json) VALUES (?, ?, ?)",
                (session_id, kind, _json(dict(payload))),
            )
            self._touch(session_id)
        row = self._connection.execute(
            "SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        assert row is not None
        return EventRecord(
            id=row["id"],
            session_id=row["session_id"],
            kind=row["kind"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
        )

    def list_events(self, session_id: str) -> list[EventRecord]:
        rows = self._connection.execute(
            "SELECT * FROM events WHERE session_id = ? ORDER BY id", (session_id,)
        )
        return [
            EventRecord(
                id=row["id"],
                session_id=row["session_id"],
                kind=row["kind"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def save_compaction(
        self,
        session_id: str,
        *,
        through_message_id: int,
        summary: Mapping[str, Any],
        retained_from_message_id: int | None = None,
    ) -> CompactionRecord:
        with self.transaction():
            cursor = self._connection.execute(
                """
                INSERT INTO compactions(
                    session_id, through_message_id, retained_from_message_id, summary_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    session_id,
                    through_message_id,
                    retained_from_message_id,
                    _json(dict(summary)),
                ),
            )
            self._touch(session_id)
        compaction_id = cursor.lastrowid
        assert compaction_id is not None
        return self.get_compaction(compaction_id)

    def get_compaction(self, compaction_id: int) -> CompactionRecord:
        row = self._connection.execute(
            "SELECT * FROM compactions WHERE id = ?", (compaction_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown compaction: {compaction_id}")
        return self._compaction_record(row)

    def latest_compaction(self, session_id: str) -> CompactionRecord | None:
        row = self._connection.execute(
            "SELECT * FROM compactions WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return None if row is None else self._compaction_record(row)

    def list_compactions(self, session_id: str) -> list[CompactionRecord]:
        rows = self._connection.execute(
            "SELECT * FROM compactions WHERE session_id = ? ORDER BY id", (session_id,)
        )
        return [self._compaction_record(row) for row in rows]

    def create_edit_transaction(
        self,
        session_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> EditTransactionRecord:
        with self.transaction():
            cursor = self._connection.execute(
                """
                INSERT INTO edit_transactions(session_id, metadata_json)
                VALUES (?, ?)
                """,
                (session_id, _json(dict(metadata or {}))),
            )
            self._touch(session_id)
        transaction_id = cursor.lastrowid
        assert transaction_id is not None
        return self.get_edit_transaction(transaction_id)

    def complete_edit_transaction(
        self,
        transaction_id: int,
        *,
        status: str = "completed",
        metadata: Mapping[str, Any] | None = None,
    ) -> EditTransactionRecord:
        with self.transaction():
            current = self._connection.execute(
                "SELECT session_id, status, metadata_json FROM edit_transactions WHERE id = ?",
                (transaction_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown edit transaction: {transaction_id}")
            if current["status"] != "open":
                raise ValueError(f"edit transaction is already complete: {transaction_id}")
            merged_metadata = json.loads(current["metadata_json"])
            if metadata is not None:
                merged_metadata.update(metadata)
            self._connection.execute(
                """
                UPDATE edit_transactions
                SET status = ?, metadata_json = ?,
                    completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (status, _json(merged_metadata), transaction_id),
            )
            self._touch(current["session_id"])
        return self.get_edit_transaction(transaction_id)

    def get_edit_transaction(self, transaction_id: int) -> EditTransactionRecord:
        row = self._connection.execute(
            "SELECT * FROM edit_transactions WHERE id = ?", (transaction_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown edit transaction: {transaction_id}")
        return self._edit_transaction_record(row)

    def list_edit_transactions(self, session_id: str) -> list[EditTransactionRecord]:
        rows = self._connection.execute(
            "SELECT * FROM edit_transactions WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
        return [self._edit_transaction_record(row) for row in rows]

    def save_snapshot(
        self,
        edit_transaction_id: int,
        path: str,
        metadata: Mapping[str, Any],
    ) -> SnapshotRecord:
        with self.transaction():
            cursor = self._connection.execute(
                """
                INSERT INTO snapshots(edit_transaction_id, path, metadata_json)
                VALUES (?, ?, ?)
                """,
                (edit_transaction_id, path, _json(dict(metadata))),
            )
        snapshot_id = cursor.lastrowid
        assert snapshot_id is not None
        return self.get_snapshot(snapshot_id)

    def get_snapshot(self, snapshot_id: int) -> SnapshotRecord:
        row = self._connection.execute(
            "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown snapshot: {snapshot_id}")
        return self._snapshot_record(row)

    def list_snapshots(self, edit_transaction_id: int) -> list[SnapshotRecord]:
        rows = self._connection.execute(
            "SELECT * FROM snapshots WHERE edit_transaction_id = ? ORDER BY id",
            (edit_transaction_id,),
        )
        return [self._snapshot_record(row) for row in rows]

    def save_claim(
        self,
        session_id: str,
        statement: str,
        status: str,
        *,
        edit_transaction_id: int | None = None,
    ) -> ClaimRecord:
        with self.transaction():
            cursor = self._connection.execute(
                """
                INSERT INTO claims(session_id, edit_transaction_id, statement, status)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, edit_transaction_id, statement, status),
            )
            self._touch(session_id)
        claim_id = cursor.lastrowid
        assert claim_id is not None
        return self.get_claim(claim_id)

    def get_claim(self, claim_id: int) -> ClaimRecord:
        row = self._connection.execute(
            "SELECT * FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown claim: {claim_id}")
        return self._claim_record(row)

    def list_claims(
        self,
        session_id: str,
        *,
        edit_transaction_id: int | None = None,
    ) -> list[ClaimRecord]:
        if edit_transaction_id is None:
            rows = self._connection.execute(
                "SELECT * FROM claims WHERE session_id = ? ORDER BY id",
                (session_id,),
            )
        else:
            rows = self._connection.execute(
                """
                SELECT * FROM claims
                WHERE session_id = ? AND edit_transaction_id = ?
                ORDER BY id
                """,
                (session_id, edit_transaction_id),
            )
        return [self._claim_record(row) for row in rows]

    def save_evidence(
        self,
        claim_id: int,
        kind: str,
        description: str,
        *,
        reference: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> EvidenceRecord:
        with self.transaction():
            cursor = self._connection.execute(
                """
                INSERT INTO evidence(
                    claim_id, kind, description, reference, metadata_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    kind,
                    description,
                    reference,
                    _json(dict(metadata or {})),
                ),
            )
        evidence_id = cursor.lastrowid
        assert evidence_id is not None
        return self.get_evidence(evidence_id)

    def get_evidence(self, evidence_id: int) -> EvidenceRecord:
        row = self._connection.execute(
            "SELECT * FROM evidence WHERE id = ?", (evidence_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown evidence: {evidence_id}")
        return self._evidence_record(row)

    def list_evidence(self, claim_id: int) -> list[EvidenceRecord]:
        rows = self._connection.execute(
            "SELECT * FROM evidence WHERE claim_id = ? ORDER BY id", (claim_id,)
        )
        return [self._evidence_record(row) for row in rows]

    def _touch(self, session_id: str) -> None:
        self._connection.execute(
            """
            UPDATE sessions
            SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (session_id,),
        )

    @staticmethod
    def _message_record(row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            id=row["id"],
            session_id=row["session_id"],
            sequence=row["sequence"],
            message=Message(
                role=row["role"],
                content=row["content"],
                tool_call_id=row["tool_call_id"],
                tool_calls=json.loads(row["tool_calls_json"]),
            ),
            created_at=row["created_at"],
        )

    @staticmethod
    def _compaction_record(row: sqlite3.Row) -> CompactionRecord:
        return CompactionRecord(
            id=row["id"],
            session_id=row["session_id"],
            through_message_id=row["through_message_id"],
            retained_from_message_id=row["retained_from_message_id"],
            summary=json.loads(row["summary_json"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _edit_transaction_record(row: sqlite3.Row) -> EditTransactionRecord:
        return EditTransactionRecord(
            id=row["id"],
            session_id=row["session_id"],
            status=row["status"],
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _snapshot_record(row: sqlite3.Row) -> SnapshotRecord:
        return SnapshotRecord(
            id=row["id"],
            edit_transaction_id=row["edit_transaction_id"],
            path=row["path"],
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _claim_record(row: sqlite3.Row) -> ClaimRecord:
        return ClaimRecord(
            id=row["id"],
            session_id=row["session_id"],
            edit_transaction_id=row["edit_transaction_id"],
            statement=row["statement"],
            status=row["status"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _evidence_record(row: sqlite3.Row) -> EvidenceRecord:
        return EvidenceRecord(
            id=row["id"],
            claim_id=row["claim_id"],
            kind=row["kind"],
            description=row["description"],
            reference=row["reference"],
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
        )
