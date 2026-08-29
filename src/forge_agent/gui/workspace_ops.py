"""Local workspace helpers for the graphical frontend."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from forge_agent.agent.tool_runtime import PersistentToolRuntime
from forge_agent.safety import PolicyEngine, PolicyToolRuntime
from forge_agent.storage import SQLiteStorage
from forge_agent.tools import build_default_registry
from forge_agent.tools.filesystem import iter_visible_paths
from forge_agent.tools.workspace import WorkspaceSandbox
from forge_agent.types import RunMode

_MAX_TREE = 1_500
_MAX_FILE_BYTES = 400_000


def pick_directory() -> str | None:
    """Open a native folder dialog and return the selected path."""

    if sys.platform == "win32":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$dialog.Description = 'Select a workspace folder'; "
            "$dialog.ShowNewFolderButton = $true; "
            "if ($dialog.ShowDialog() -eq 'OK') { "
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$dialog.SelectedPath }"
        )
        completed = subprocess.run(
            ["powershell", "-STA", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        selected = completed.stdout.strip()
        return selected or None
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    selected = filedialog.askdirectory()
    root.destroy()
    return selected or None


def workspace_tree(workspace: Path) -> list[dict[str, Any]]:
    sandbox = WorkspaceSandbox(workspace)
    nodes: dict[str, dict[str, Any]] = {
        "": {"name": workspace.name, "path": "", "kind": "dir", "children": []}
    }
    for count, item in enumerate(
        iter_visible_paths(sandbox, sandbox.root, recursive=True, include_directories=True),
        start=1,
    ):
        if count > _MAX_TREE:
            break
        relative = sandbox.relative(item)
        parent = str(Path(relative).parent).replace("\\", "/")
        if parent == ".":
            parent = ""
        parent_node = nodes.setdefault(
            parent,
            {
                "name": Path(parent).name or workspace.name,
                "path": parent,
                "kind": "dir",
                "children": [],
            },
        )
        node: dict[str, Any] = {
            "name": item.name,
            "path": relative,
            "kind": "dir" if item.is_dir() else "file",
            "children": [],
        }
        nodes[relative] = node
        parent_node["children"].append(node)
    return cast(list[dict[str, Any]], nodes[""]["children"])


def read_workspace_file(
    workspace: Path,
    relative: str,
    *,
    diff: str | None = None,
) -> dict[str, Any]:
    sandbox = WorkspaceSandbox(workspace)
    path = sandbox.resolve(relative, must_exist=True)
    if not path.is_file():
        raise ValueError(f"not a file: {relative}")
    size = path.stat().st_size
    if size > _MAX_FILE_BYTES:
        return {
            "path": relative,
            "content": "",
            "binary": False,
            "truncated": True,
            "diff": diff,
        }
    data = path.read_bytes()
    if b"\0" in data[:8_192]:
        return {
            "path": relative,
            "content": "",
            "binary": True,
            "truncated": False,
            "diff": diff,
        }
    return {
        "path": relative,
        "content": data.decode("utf-8", errors="replace"),
        "binary": False,
        "truncated": False,
        "diff": diff,
    }


async def write_workspace_file(
    workspace: Path,
    relative: str,
    content: str,
    *,
    database_path: Path | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    del database_path, session_id
    if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
        return {
            "ok": False,
            "summary": "文件过大，无法保存。",
            "error_code": "too_large",
        }
    sandbox = WorkspaceSandbox(workspace)
    path = sandbox.resolve(relative, must_exist=True)
    if not path.is_file():
        raise ValueError(f"not a file: {relative}")
    if b"\0" in path.read_bytes()[:8_192]:
        return {
            "ok": False,
            "summary": "二进制文件不能在编辑器中保存。",
            "error_code": "binary",
        }
    sandbox.atomic_write(path, content)
    return {"ok": True, "summary": f"已保存 {relative}", "error_code": None}


def create_workspace_file(
    workspace: Path,
    relative: str,
    content: str = "",
) -> dict[str, Any]:
    if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
        return {
            "ok": False,
            "summary": "文件过大，无法创建。",
            "error_code": "too_large",
        }
    sandbox = WorkspaceSandbox(workspace)
    path = sandbox.resolve(relative, must_exist=False)
    if path.exists() and path.is_dir():
        raise ValueError(f"already a directory: {relative}")
    if path.exists() and path.is_file():
        return {"ok": False, "summary": f"{relative} 已存在", "error_code": "exists"}
    sandbox.atomic_write(path, content)
    return {"ok": True, "summary": f"已创建 {relative}", "error_code": None}


def latest_diff_for_path(events: list[Any], relative: str) -> str | None:
    needle = relative.replace("\\", "/")
    found: str | None = None
    for event in events:
        payload = event.payload if hasattr(event, "payload") else event
        if not isinstance(payload, dict):
            continue
        metadata = payload.get("metadata")
        matches = False
        if isinstance(metadata, dict):
            changed = metadata.get("changed_files")
            if isinstance(changed, list) and needle in [
                str(item).replace("\\", "/") for item in changed
            ]:
                matches = True
        arguments = payload.get("arguments")
        if (
            isinstance(arguments, dict)
            and str(arguments.get("path", "")).replace("\\", "/") == needle
        ):
            matches = True
        content = payload.get("content")
        if not isinstance(content, str):
            continue
        if matches and (content.startswith("--- ") or content.startswith("diff ")):
            found = content
    return found


def undo_path(
    database_path: Path,
    session_id: str,
    workspace: Path,
    relative: str,
) -> dict[str, Any]:
    normalized = relative.replace("\\", "/")
    with SQLiteStorage(database_path) as storage:
        runtime = PersistentToolRuntime(
            PolicyToolRuntime(
                build_default_registry(workspace),
                PolicyEngine(mode=RunMode.BUILD, auto_approve=True),
            ),
            storage,
            session_id,
            workspace,
        )
        transaction = None
        for candidate in reversed(runtime._rollback_candidates()):
            candidate_path = str(candidate.metadata.get("path", "")).replace("\\", "/")
            if candidate_path == normalized:
                transaction = candidate
                break
        if transaction is None:
            return {
                "ok": False,
                "summary": f"没有可撤销的 {relative}",
                "error_code": "no_edit",
            }
        result = runtime._restore_group([transaction])
    return {
        "ok": result.ok,
        "summary": result.summary,
        "error_code": result.error_code,
        "content": result.content,
    }


def session_settings_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    max_cost = metadata.get("max_cost")
    return {
        "workspace": str(metadata.get("workspace", "")),
        "model": str(metadata.get("model", "")),
        "mode": str(metadata.get("mode", "build")),
        "verify": str(metadata.get("verify_command") or ""),
        "max_steps": int(metadata.get("max_steps") or 30),
        "max_tokens": int(metadata.get("max_tokens") or 1_000_000),
        "max_cost": "" if max_cost in {None, ""} else str(max_cost),
        "auto_approve": bool(metadata.get("auto_approve", False)),
        "demo": bool(metadata.get("demo", False)),
    }


def apply_session_settings(metadata: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    updated = dict(metadata)
    if values.get("model"):
        updated["model"] = values["model"]
    if values.get("mode"):
        updated["mode"] = values["mode"]
    if "verify" in values:
        updated["verify_command"] = values["verify"] or None
    if "max_steps" in values:
        updated["max_steps"] = values["max_steps"]
    if "max_tokens" in values:
        updated["max_tokens"] = values["max_tokens"]
    if "max_cost" in values:
        updated["max_cost"] = values["max_cost"]
    if "auto_approve" in values:
        updated["auto_approve"] = values["auto_approve"]
    if "demo" in values:
        updated["demo"] = values["demo"]
    if values.get("title"):
        updated["task"] = str(values["title"]).strip() or updated.get("task", "")
    return updated


def accepted_diffs_from_metadata(metadata: dict[str, Any]) -> dict[str, str]:
    raw = metadata.get("accepted_diffs")
    if not isinstance(raw, dict):
        return {}
    return {
        str(path): str(diff)
        for path, diff in raw.items()
        if isinstance(path, str) and isinstance(diff, str)
    }


def apply_accepted_diffs(metadata: dict[str, Any], diffs: dict[str, str]) -> dict[str, Any]:
    updated = dict(metadata)
    updated["accepted_diffs"] = dict(diffs)
    return updated
