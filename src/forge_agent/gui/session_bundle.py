"""Export and import a GUI session as JSON so it can be reused later."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forge_agent.storage.sqlite import SQLiteStorage
from forge_agent.types import Message

BUNDLE_FORMAT = "forge-agent.session"
BUNDLE_VERSION = 2
_SUPPORTED_VERSIONS = frozenset({1, 2})
_MAX_BACKUP_BYTES = 5_000_000


class SessionBundleError(ValueError):
    """User-visible export/import error."""


def export_session_bundle(database_path: Path, session_id: str) -> dict[str, Any]:
    with SQLiteStorage(database_path) as storage:
        record = storage.get_session(session_id)
        if record is None:
            raise SessionBundleError("找不到这个会话")
        messages = storage.list_messages(session_id)
        events = storage.list_events(session_id)
        compactions = storage.list_compactions(session_id)
        claims_out: list[dict[str, Any]] = []
        for claim in storage.list_claims(session_id):
            claims_out.append(
                {
                    "statement": claim.statement,
                    "status": claim.status,
                    "evidence": [
                        {
                            "kind": item.kind,
                            "description": item.description,
                            "reference": item.reference,
                            "metadata": item.metadata,
                        }
                        for item in storage.list_evidence(claim.id)
                    ],
                }
            )
        id_to_sequence = {item.id: item.sequence for item in messages}
        compaction_out = []
        for item in compactions:
            through = id_to_sequence.get(item.through_message_id)
            if through is None:
                continue
            retained = (
                id_to_sequence.get(item.retained_from_message_id)
                if item.retained_from_message_id is not None
                else None
            )
            compaction_out.append(
                {
                    "through_sequence": through,
                    "retained_from_sequence": retained,
                    "summary": item.summary,
                }
            )
        return {
            "format": BUNDLE_FORMAT,
            "version": BUNDLE_VERSION,
            "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_session_id": record.id,
            "session": {
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "metadata": dict(record.metadata),
            },
            "messages": [
                {
                    "sequence": item.sequence,
                    "role": item.message.role,
                    "content": item.message.content,
                    "tool_call_id": item.message.tool_call_id,
                    "tool_calls": [call.model_dump(mode="json") for call in item.message.tool_calls],
                    "created_at": item.created_at,
                }
                for item in messages
            ],
            "events": [
                {"kind": item.kind, "payload": item.payload, "created_at": item.created_at}
                for item in events
            ],
            "claims": claims_out,
            "compactions": compaction_out,
            "edit_transactions": _export_edits(storage, record.id),
        }


def bundle_filename(bundle: dict[str, Any]) -> str:
    source = re.sub(r"[^A-Za-z0-9]", "", str(bundle.get("source_session_id") or "export"))[:8] or "export"
    return f"forge-session-{source}.json"


def import_session_bundle(database_path: Path, payload: dict[str, Any]) -> str:
    if payload.get("format") != BUNDLE_FORMAT:
        raise SessionBundleError("这不是 ForgeAgent 会话导出文件")
    version = payload.get("version", 1)
    if version not in _SUPPORTED_VERSIONS:
        raise SessionBundleError(f"不支持的导出版本：{version}")
    session_block = payload.get("session")
    if not isinstance(session_block, dict):
        raise SessionBundleError("导出文件缺少会话信息")
    metadata = session_block.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    messages = payload.get("messages") or []
    events = payload.get("events") or []
    claims = payload.get("claims") or []
    compactons = payload.get("compactions") or []
    if not isinstance(messages, list) or not isinstance(events, list):
        raise SessionBundleError("导出文件里的对话记录格式不对")
    if not isinstance(claims, list) or not isinstance(compactons, list):
        raise SessionBundleError("导出文件里的核对记录格式不对")

    with SQLiteStorage(database_path) as storage:
        with storage.transaction():
            created = storage.create_session(metadata=metadata)
            sequence_to_id: dict[int, int] = {}
            for index, raw in enumerate(messages):
                if not isinstance(raw, dict):
                    raise SessionBundleError(f"第 {index + 1} 条消息格式不对")
                try:
                    message = Message.model_validate(
                        {
                            "role": raw.get("role") or "user",
                            "content": raw.get("content"),
                            "tool_call_id": raw.get("tool_call_id"),
                            "tool_calls": raw.get("tool_calls") or [],
                        }
                    )
                except (ValueError, TypeError) as exc:
                    raise SessionBundleError(f"第 {index + 1} 条消息无法读取") from exc
                stored = storage.append_message(created.id, message)
                sequence_to_id[int(raw.get("sequence") or stored.sequence)] = stored.id
            for raw in events:
                if not isinstance(raw, dict):
                    continue
                kind = str(raw.get("kind") or "").strip()
                if not kind:
                    continue
                body = raw.get("payload")
                storage.append_event(created.id, kind, body if isinstance(body, dict) else {})
            for raw in claims:
                if not isinstance(raw, dict):
                    continue
                statement = str(raw.get("statement") or "").strip()
                if not statement:
                    continue
                claim = storage.save_claim(
                    created.id,
                    statement,
                    str(raw.get("status") or "unverified"),
                )
                evidence = raw.get("evidence") or []
                if not isinstance(evidence, list):
                    continue
                for item in evidence:
                    if not isinstance(item, dict):
                        continue
                    storage.save_evidence(
                        claim.id,
                        str(item.get("kind") or "note"),
                        str(item.get("description") or ""),
                        reference=item.get("reference"),
                        metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                    )
            for raw in compactons:
                if not isinstance(raw, dict):
                    continue
                through_seq = raw.get("through_sequence")
                through_id = sequence_to_id.get(int(through_seq)) if through_seq is not None else None
                if through_id is None:
                    continue
                retained_seq = raw.get("retained_from_sequence")
                retained_id = (
                    sequence_to_id.get(int(retained_seq)) if retained_seq is not None else None
                )
                summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
                storage.save_compaction(
                    created.id,
                    through_message_id=through_id,
                    summary=summary,
                    retained_from_message_id=retained_id,
                )
            _import_edits(storage, created.id, payload.get("edit_transactions") or [])
            return created.id


def dump_bundle_json(bundle: dict[str, Any]) -> bytes:
    text = json.dumps(bundle, ensure_ascii=False, indent=2)
    return (text + "\n").encode("utf-8")


def _export_edits(storage: SQLiteStorage, session_id: str) -> list[dict[str, Any]]:
    exported: list[dict[str, Any]] = []
    for transaction in storage.list_edit_transactions(session_id):
        snapshots_out: list[dict[str, Any]] = []
        for snapshot in storage.list_snapshots(transaction.id):
            metadata = dict(snapshot.metadata)
            backup_b64 = None
            backup = metadata.get("backup_path")
            if isinstance(backup, str) and backup:
                path = Path(backup)
                if path.is_file() and path.stat().st_size <= _MAX_BACKUP_BYTES:
                    backup_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            metadata.pop("backup_path", None)
            snapshots_out.append(
                {
                    "path": snapshot.path,
                    "metadata": metadata,
                    "backup_b64": backup_b64,
                }
            )
        exported.append(
            {
                "id": transaction.id,
                "status": transaction.status,
                "metadata": dict(transaction.metadata),
                "snapshots": snapshots_out,
            }
        )
    return exported


def _remap_edit_metadata(metadata: dict[str, Any], id_map: dict[int, int]) -> dict[str, Any]:
    updated = dict(metadata)
    rollback = updated.get("rolls_back")
    if isinstance(rollback, int) and rollback in id_map:
        updated["rolls_back"] = id_map[rollback]
    many = updated.get("rolls_back_many")
    if isinstance(many, list):
        updated["rolls_back_many"] = [
            id_map[item] if isinstance(item, int) and item in id_map else item for item in many
        ]
    return updated


def _import_edits(storage: SQLiteStorage, session_id: str, rows: Any) -> None:
    if not isinstance(rows, list):
        return
    snapshot_root = storage.path.parent / "snapshots" / session_id
    id_map: dict[int, int] = {}
    prepared: list[tuple[int, dict[str, Any]]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        created = storage.create_edit_transaction(session_id, metadata={})
        old_id = raw.get("id")
        if isinstance(old_id, int):
            id_map[old_id] = created.id
        prepared.append((created.id, raw))
    for new_id, raw in prepared:
        snapshots = raw.get("snapshots") or []
        if isinstance(snapshots, list):
            for item in snapshots:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "")
                if not path:
                    continue
                metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                meta = dict(metadata)
                encoded = item.get("backup_b64")
                if isinstance(encoded, str) and encoded:
                    directory = snapshot_root / str(new_id)
                    directory.mkdir(parents=True, exist_ok=True)
                    backup = directory / f"{hashlib.sha256(path.encode()).hexdigest()}.bak"
                    backup.write_bytes(base64.b64decode(encoded, validate=False))
                    meta["backup_path"] = str(backup)
                    meta["existed"] = True
                storage.save_snapshot(new_id, path, meta)
        status = str(raw.get("status") or "completed")
        if status == "open":
            status = "completed"
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        storage.complete_edit_transaction(
            new_id,
            status=status,
            metadata=_remap_edit_metadata(dict(metadata), id_map),
        )
