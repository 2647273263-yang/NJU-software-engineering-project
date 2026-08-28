"""Persistence wrapper for edit transactions and cross-restart undo."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from forge_agent.safety import PolicyToolRuntime
from forge_agent.storage import EditTransactionRecord, SnapshotRecord, SQLiteStorage
from forge_agent.tools.workspace import WorkspaceSandbox
from forge_agent.types import ToolCall, ToolResult

_EDIT_TOOLS = frozenset({"replace_in_file", "write_file"})


class PersistentToolRuntime:
    def __init__(
        self,
        runtime: PolicyToolRuntime,
        storage: SQLiteStorage,
        session_id: str,
        workspace: Path,
    ) -> None:
        self.runtime = runtime
        self.storage = storage
        self.session_id = session_id
        self.sandbox = WorkspaceSandbox(workspace)
        self.snapshot_root = storage.path.parent / "snapshots" / session_id

    def schemas(self) -> list[dict[str, Any]]:
        return self.runtime.schemas()

    async def execute(self, call: ToolCall) -> ToolResult:
        if call.name == "rollback_changes":
            candidates = self._rollback_candidates()
            result = await self.runtime.execute(call)
            if result.ok:
                self._record_group_rollback(candidates)
                return result
            if result.error_code not in {"ValueError", "no_edit"}:
                return result
            return self._restore_group(candidates)
        if call.name == "undo_last_edit":
            result = await self.runtime.execute(call)
            if result.ok:
                self._record_rollback(self._latest_restorable())
                return result
            if result.error_code not in {"ValueError", "no_edit"}:
                return result
            return self._restore_latest()
        if call.name not in _EDIT_TOOLS:
            result = await self.runtime.execute(call)
            if call.name == "verify_changes" and result.ok:
                self._record_verification_checkpoint()
            return result

        relative = str(call.arguments.get("path", ""))
        try:
            path = self.sandbox.resolve(relative)
        except (ValueError, OSError) as exc:
            return ToolResult(
                ok=False,
                summary=str(exc),
                error_code="workspace_violation",
            )
        transaction = self.storage.create_edit_transaction(
            self.session_id,
            metadata={"tool": call.name, "path": relative},
        )
        snapshot = self._save_snapshot(transaction, path, relative)
        result = await self.runtime.execute(call)
        metadata = {
            "tool": call.name,
            "path": relative,
            "result_ok": result.ok,
            "after_sha256": result.metadata.get("sha256"),
            "snapshot_id": snapshot.id,
        }
        self.storage.complete_edit_transaction(
            transaction.id,
            status="completed" if result.ok else "failed",
            metadata=metadata,
        )
        result.metadata["edit_transaction_id"] = transaction.id
        return result

    def _save_snapshot(
        self,
        transaction: EditTransactionRecord,
        path: Path,
        relative: str,
    ) -> SnapshotRecord:
        existed = path.is_file()
        before_sha256 = None
        backup_path = None
        if existed:
            content = path.read_bytes()
            before_sha256 = hashlib.sha256(content).hexdigest()
            directory = self.snapshot_root / str(transaction.id)
            directory.mkdir(parents=True, exist_ok=True)
            backup = directory / f"{hashlib.sha256(relative.encode()).hexdigest()}.bak"
            shutil.copy2(path, backup)
            backup_path = str(backup)
        return self.storage.save_snapshot(
            transaction.id,
            relative,
            {
                "existed": existed,
                "before_sha256": before_sha256,
                "backup_path": backup_path,
            },
        )

    def _latest_restorable(self) -> EditTransactionRecord | None:
        transactions = self.storage.list_edit_transactions(self.session_id)
        rolled_back = {
            int(item.metadata["rolls_back"])
            for item in transactions
            if "rolls_back" in item.metadata
        }
        for transaction in reversed(transactions):
            if (
                transaction.status == "completed"
                and transaction.id not in rolled_back
                and transaction.metadata.get("result_ok") is True
                and transaction.metadata.get("tool") in _EDIT_TOOLS
            ):
                return transaction
        return None

    def _restore_latest(self) -> ToolResult:
        transaction = self._latest_restorable()
        if transaction is None:
            return ToolResult(
                ok=False,
                summary="there is no persisted edit to undo",
                error_code="no_edit",
            )
        snapshots = self.storage.list_snapshots(transaction.id)
        if not snapshots:
            return ToolResult(
                ok=False,
                summary="the edit transaction has no snapshot",
                error_code="missing_snapshot",
            )
        snapshot = snapshots[-1]
        path = self.sandbox.resolve(snapshot.path)
        expected_after = transaction.metadata.get("after_sha256")
        current_sha = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
        if expected_after != current_sha:
            return ToolResult(
                ok=False,
                summary="file changed after the persisted edit; refusing to overwrite it",
                error_code="concurrent_modification",
            )
        if snapshot.metadata.get("existed"):
            backup = Path(str(snapshot.metadata["backup_path"]))
            if not backup.is_file():
                return ToolResult(
                    ok=False,
                    summary="snapshot backup is missing",
                    error_code="missing_snapshot",
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, path)
        else:
            path.unlink(missing_ok=True)
        self._record_rollback(transaction)
        return ToolResult(
            ok=True,
            summary=f"restored persisted snapshot for {snapshot.path}",
            metadata={
                "changed_files": [snapshot.path],
                "undo": True,
                "rolls_back": transaction.id,
            },
        )

    def _record_rollback(self, transaction: EditTransactionRecord | None) -> None:
        if transaction is None:
            return
        rollback = self.storage.create_edit_transaction(
            self.session_id,
            metadata={"tool": "undo_last_edit", "rolls_back": transaction.id},
        )
        self.storage.complete_edit_transaction(
            rollback.id,
            status="completed",
            metadata={"result_ok": True, "rolls_back": transaction.id},
        )

    def _rollback_candidates(self) -> list[EditTransactionRecord]:
        transactions = self.storage.list_edit_transactions(self.session_id)
        rolled_back: set[int] = set()
        checkpoint = 0
        for transaction in transactions:
            if transaction.metadata.get("tool") == "verification_checkpoint":
                checkpoint = transaction.id
            rollback = transaction.metadata.get("rolls_back")
            if isinstance(rollback, int):
                rolled_back.add(rollback)
            many = transaction.metadata.get("rolls_back_many")
            if isinstance(many, list):
                rolled_back.update(item for item in many if isinstance(item, int))
        return [
            transaction
            for transaction in transactions
            if transaction.id > checkpoint
            and transaction.id not in rolled_back
            and transaction.status == "completed"
            and transaction.metadata.get("result_ok") is True
            and transaction.metadata.get("tool") in _EDIT_TOOLS
        ]

    def _restore_group(
        self,
        transactions: list[EditTransactionRecord],
    ) -> ToolResult:
        if not transactions:
            return ToolResult(
                ok=False,
                summary="there are no unverified edits to roll back",
                error_code="no_edit_group",
            )
        grouped: dict[str, tuple[SnapshotRecord, str | None]] = {}
        for transaction in transactions:
            snapshots = self.storage.list_snapshots(transaction.id)
            if not snapshots:
                return ToolResult(
                    ok=False,
                    summary=f"transaction {transaction.id} has no snapshot",
                    error_code="missing_snapshot",
                )
            snapshot = snapshots[-1]
            first, _ = grouped.get(
                snapshot.path,
                (snapshot, transaction.metadata.get("after_sha256")),
            )
            expected = transaction.metadata.get("after_sha256")
            grouped[snapshot.path] = (
                first,
                str(expected) if expected is not None else None,
            )
        for path_text, (_, expected_after) in grouped.items():
            path = self.sandbox.resolve(path_text)
            current_sha = (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file()
                else None
            )
            if current_sha != expected_after:
                return ToolResult(
                    ok=False,
                    summary=(
                        f"{path_text} changed after the edit group; "
                        "refusing to overwrite it"
                    ),
                    error_code="concurrent_modification",
                )
        changed: list[str] = []
        for path_text, (snapshot, _) in reversed(list(grouped.items())):
            path = self.sandbox.resolve(path_text)
            if snapshot.metadata.get("existed"):
                backup = Path(str(snapshot.metadata.get("backup_path", "")))
                if not backup.is_file():
                    return ToolResult(
                        ok=False,
                        summary=f"snapshot backup is missing for {path_text}",
                        error_code="missing_snapshot",
                    )
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, path)
            else:
                path.unlink(missing_ok=True)
            changed.append(path_text)
        self._record_group_rollback(transactions)
        return ToolResult(
            ok=True,
            summary=f"restored {len(changed)} file(s) from the edit group",
            metadata={
                "changed_files": changed,
                "rollback_group": True,
                "rolls_back_many": [item.id for item in transactions],
            },
        )

    def _record_group_rollback(
        self,
        transactions: list[EditTransactionRecord],
    ) -> None:
        if not transactions:
            return
        identifiers = [transaction.id for transaction in transactions]
        rollback = self.storage.create_edit_transaction(
            self.session_id,
            metadata={"tool": "rollback_changes", "rolls_back_many": identifiers},
        )
        self.storage.complete_edit_transaction(
            rollback.id,
            status="completed",
            metadata={"result_ok": True, "rolls_back_many": identifiers},
        )

    def _record_verification_checkpoint(self) -> None:
        checkpoint = self.storage.create_edit_transaction(
            self.session_id,
            metadata={"tool": "verification_checkpoint"},
        )
        self.storage.complete_edit_transaction(
            checkpoint.id,
            status="completed",
            metadata={"result_ok": True, "tool": "verification_checkpoint"},
        )
