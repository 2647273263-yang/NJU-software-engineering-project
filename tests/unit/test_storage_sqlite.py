from __future__ import annotations

import sqlite3

import pytest

from forge_agent.storage import SQLiteStorage
from forge_agent.types import Message, ToolCall


def test_persists_sessions_messages_events_and_compactions(tmp_path) -> None:
    database = tmp_path / "state.db"
    with SQLiteStorage(database) as storage:
        session = storage.create_session("session-1", {"workspace": "demo"})
        first = storage.append_message("session-1", Message(role="user", content="hello"))
        second = storage.append_message(
            "session-1",
            Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id="call-1", name="read", arguments={"path": "a.py"})],
            ),
        )
        event = storage.append_event("session-1", "tool.started", {"name": "read"})
        compaction = storage.save_compaction(
            "session-1",
            through_message_id=first.id,
            retained_from_message_id=second.id,
            summary={"goal": "finish"},
        )

        assert session.metadata == {"workspace": "demo"}
        assert storage.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert [record.message.role for record in storage.list_messages("session-1")] == [
            "user",
            "assistant",
        ]
        assert storage.get_message(second.id).message.tool_calls[0].name == "read"
        assert storage.list_events("session-1") == [event]
        assert storage.latest_compaction("session-1") == compaction
        assert len(storage.list_messages("session-1")) == 2

    with SQLiteStorage(database) as reopened:
        assert reopened.get_session("session-1") is not None
        assert len(reopened.list_messages("session-1")) == 2


def test_replace_messages_rewrites_history_despite_compaction_foreign_keys(
    tmp_path,
) -> None:
    with SQLiteStorage(tmp_path / "state.db") as storage:
        storage.create_session("session-1")
        first = storage.append_message("session-1", Message(role="user", content="edit"))
        storage.append_message(
            "session-1",
            Message(
                role="assistant",
                tool_calls=[ToolCall(id="call-1", name="write_file", arguments={})],
            ),
        )
        storage.save_compaction(
            "session-1",
            through_message_id=first.id,
            retained_from_message_id=None,
            summary={"goal": "keep going"},
        )

        storage.replace_messages(
            "session-1",
            [
                Message(role="user", content="edit"),
                Message(
                    role="assistant",
                    tool_calls=[ToolCall(id="call-1", name="write_file", arguments={})],
                ),
                Message(
                    role="tool",
                    tool_call_id="call-1",
                    content="write_file was interrupted before a result was recorded.",
                ),
            ],
        )

        roles = [record.message.role for record in storage.list_messages("session-1")]
        assert roles == ["user", "assistant", "tool"]
        assert storage.latest_compaction("session-1") is None


def test_transaction_rolls_back_nested_repository_calls(tmp_path) -> None:
    with SQLiteStorage(tmp_path / "state.db") as storage:
        storage.create_session("session-1")

        with pytest.raises(RuntimeError, match="abort"), storage.transaction():
            storage.append_message("session-1", Message(role="user", content="temporary"))
            storage.append_event("session-1", "temporary", {})
            raise RuntimeError("abort")

        assert storage.list_messages("session-1") == []
        assert storage.list_events("session-1") == []


def test_foreign_keys_reject_unknown_session(tmp_path) -> None:
    with SQLiteStorage(tmp_path / "state.db") as storage, pytest.raises(
        sqlite3.IntegrityError
    ):
        storage.append_message("missing", Message(role="user", content="hello"))


def test_persists_edit_transactions_snapshots_claims_and_evidence(tmp_path) -> None:
    database = tmp_path / "state.db"
    with SQLiteStorage(database) as storage:
        storage.create_session("session-1")
        edit = storage.create_edit_transaction("session-1", {"request": "rename"})
        snapshot = storage.save_snapshot(
            edit.id,
            "src/example.py",
            {"before_sha256": "aaa", "after_sha256": "bbb", "existed": True},
        )
        claim = storage.save_claim(
            "session-1",
            "The file was renamed",
            "proven",
            edit_transaction_id=edit.id,
        )
        evidence = storage.save_evidence(
            claim.id,
            "file_change",
            "src/example.py changed",
            reference="src/example.py",
            metadata={"sha256": "bbb"},
        )
        completed = storage.complete_edit_transaction(
            edit.id, metadata={"changed_files": ["src/example.py"]}
        )

        assert completed.status == "completed"
        assert completed.completed_at is not None
        assert completed.metadata == {
            "request": "rename",
            "changed_files": ["src/example.py"],
        }
        assert storage.get_snapshot(snapshot.id) == snapshot
        assert storage.get_claim(claim.id) == claim
        assert storage.get_evidence(evidence.id) == evidence

    with SQLiteStorage(database) as reopened:
        assert reopened.list_edit_transactions("session-1") == [completed]
        assert reopened.list_snapshots(edit.id) == [snapshot]
        assert reopened.list_claims("session-1") == [claim]
        assert reopened.list_evidence(claim.id) == [evidence]


def test_new_records_are_returned_in_insertion_order(tmp_path) -> None:
    with SQLiteStorage(tmp_path / "state.db") as storage:
        storage.create_session("session-1")
        first_edit = storage.create_edit_transaction("session-1")
        second_edit = storage.create_edit_transaction("session-1")
        first_snapshot = storage.save_snapshot(first_edit.id, "z.py", {"order": 1})
        second_snapshot = storage.save_snapshot(first_edit.id, "a.py", {"order": 2})
        first_claim = storage.save_claim(
            "session-1", "first", "proven", edit_transaction_id=first_edit.id
        )
        second_claim = storage.save_claim(
            "session-1", "second", "unproven", edit_transaction_id=second_edit.id
        )
        first_evidence = storage.save_evidence(first_claim.id, "command", "first")
        second_evidence = storage.save_evidence(first_claim.id, "command", "second")

        assert storage.list_edit_transactions("session-1") == [first_edit, second_edit]
        assert storage.list_snapshots(first_edit.id) == [first_snapshot, second_snapshot]
        assert storage.list_claims("session-1") == [first_claim, second_claim]
        assert storage.list_claims(
            "session-1", edit_transaction_id=second_edit.id
        ) == [second_claim]
        assert storage.list_evidence(first_claim.id) == [first_evidence, second_evidence]


def test_nested_rollback_removes_edit_graph(tmp_path) -> None:
    with SQLiteStorage(tmp_path / "state.db") as storage:
        storage.create_session("session-1")

        with pytest.raises(RuntimeError, match="abort"), storage.transaction():
            edit = storage.create_edit_transaction("session-1")
            storage.save_snapshot(edit.id, "temporary.py", {"before": "old"})
            claim = storage.save_claim(
                "session-1", "temporary claim", "unproven", edit_transaction_id=edit.id
            )
            storage.save_evidence(claim.id, "command", "temporary evidence")
            storage.complete_edit_transaction(edit.id)
            raise RuntimeError("abort")

        assert storage.list_edit_transactions("session-1") == []
        assert storage.connection.execute("SELECT * FROM snapshots").fetchall() == []
        assert storage.list_claims("session-1") == []
        assert storage.connection.execute("SELECT * FROM evidence").fetchall() == []


@pytest.mark.parametrize(
    ("operation", "expected_table"),
    [
        (lambda storage: storage.create_edit_transaction("missing"), "edit_transactions"),
        (lambda storage: storage.save_snapshot(999, "missing.py", {}), "snapshots"),
        (
            lambda storage: storage.save_claim(
                "missing", "claim", "unproven", edit_transaction_id=None
            ),
            "claims",
        ),
        (
            lambda storage: storage.save_evidence(999, "command", "missing"),
            "evidence",
        ),
    ],
)
def test_new_foreign_keys_reject_missing_parents(tmp_path, operation, expected_table) -> None:
    with SQLiteStorage(tmp_path / f"{expected_table}.db") as storage, pytest.raises(
        sqlite3.IntegrityError
    ):
        operation(storage)


def test_schema_migrates_existing_database_without_replacing_data(tmp_path) -> None:
    database = tmp_path / "existing.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO sessions VALUES ('existing', 'created', 'updated', '{\"kept\":true}')"
    )
    connection.commit()
    connection.close()

    with SQLiteStorage(database) as storage:
        assert storage.get_session("existing") is not None
        edit = storage.create_edit_transaction("existing")
        assert storage.get_edit_transaction(edit.id) == edit
        tables = {
            row[0]
            for row in storage.connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name IN (
                    'edit_transactions', 'snapshots', 'claims', 'evidence'
                )
                """
            )
        }
        assert tables == {"edit_transactions", "snapshots", "claims", "evidence"}


def test_patch_session_metadata_keeps_sibling_keys(tmp_path) -> None:
    database = tmp_path / "state.db"
    with SQLiteStorage(database) as storage:
        storage.create_session("session-1", {"task": "sort", "status": "thinking"})
        storage.patch_session_metadata(
            "session-1",
            {"accepted_diffs": {"a.py": "a.py::abc"}},
        )
        storage.patch_session_metadata("session-1", {"status": "completed"})
        meta = storage.get_session("session-1").metadata
        assert meta["task"] == "sort"
        assert meta["status"] == "completed"
        assert meta["accepted_diffs"] == {"a.py": "a.py::abc"}
