"""Local workspace helpers for the graphical frontend."""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from forge_agent.agent.tool_runtime import PersistentToolRuntime
from forge_agent.safety import PolicyEngine, PolicyToolRuntime
from forge_agent.storage import SQLiteStorage
from forge_agent.tools import build_default_registry
from forge_agent.tools.filesystem import decode_bytes, is_image_path, iter_visible_paths
from forge_agent.tools.workspace import WorkspaceSandbox
from forge_agent.types import RunMode

_MAX_TREE = 1_500
_MAX_FILE_BYTES = 2_000_000
_PREVIEW_BYTES = 400_000
_MAX_IMAGE_BYTES = 5_000_000
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


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
    if is_image_path(path):
        if size > _MAX_IMAGE_BYTES:
            return {
                "path": relative,
                "content": "",
                "binary": True,
                "image": False,
                "truncated": True,
                "encoding": None,
                "diff": diff,
            }
        data = path.read_bytes()
        mime = _IMAGE_MIME.get(path.suffix.lower(), "application/octet-stream")
        return {
            "path": relative,
            "content": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
            "binary": True,
            "image": True,
            "truncated": False,
            "encoding": None,
            "diff": diff,
        }
    data = path.read_bytes() if size <= _MAX_FILE_BYTES else path.read_bytes()[:_PREVIEW_BYTES]
    truncated = size > _MAX_FILE_BYTES
    if b"\0" in data[:8_192]:
        return {
            "path": relative,
            "content": "",
            "binary": True,
            "image": False,
            "truncated": False,
            "encoding": None,
            "diff": diff,
        }
    text, encoding = decode_bytes(data)
    if truncated:
        text = text + "\n…\n[文件过大，只显示开头]"
    return {
        "path": relative,
        "content": text,
        "binary": False,
        "image": False,
        "truncated": truncated,
        "encoding": encoding,
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


def delete_workspace_path(workspace: Path, relative: str) -> dict[str, Any]:
    sandbox = WorkspaceSandbox(workspace)
    path = sandbox.resolve(relative, must_exist=True)
    if path == sandbox.root:
        return {
            "ok": False,
            "summary": "不能删除工作区根目录。",
            "error_code": "workspace_root",
        }
    if sandbox.is_sensitive_content(path):
        return {
            "ok": False,
            "summary": f"拒绝删除敏感路径 {relative}",
            "error_code": "sensitive",
        }
    if path.is_dir():
        shutil.rmtree(path)
        return {"ok": True, "summary": f"已删除文件夹 {relative}", "error_code": None}
    path.unlink()
    return {"ok": True, "summary": f"已删除 {relative}", "error_code": None}


def rename_workspace_path(
    workspace: Path,
    relative: str,
    destination: str,
) -> dict[str, Any]:
    sandbox = WorkspaceSandbox(workspace)
    source = sandbox.resolve(relative, must_exist=True)
    target = sandbox.resolve(destination, must_exist=False)
    if source == sandbox.root:
        return {
            "ok": False,
            "summary": "不能重命名工作区根目录。",
            "error_code": "workspace_root",
        }
    if sandbox.is_sensitive_content(source) or sandbox.is_sensitive_content(target):
        return {
            "ok": False,
            "summary": "拒绝重命名敏感路径。",
            "error_code": "sensitive",
        }
    if target.exists():
        return {
            "ok": False,
            "summary": f"{destination} 已存在",
            "error_code": "exists",
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    return {
        "ok": True,
        "summary": f"已将 {relative} 重命名为 {destination}",
        "error_code": None,
        "path": sandbox.relative(target),
    }


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


def create_workspace_dir(workspace: Path, relative: str) -> dict[str, Any]:
    sandbox = WorkspaceSandbox(workspace)
    path = sandbox.resolve(relative, must_exist=False)
    if path.exists() and path.is_file():
        return {"ok": False, "summary": f"{relative} 已是文件", "error_code": "exists"}
    path.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "summary": f"已创建文件夹 {relative}", "error_code": None, "path": sandbox.relative(path)}


_UPLOAD_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_UPLOAD_MAX = 2_000_000


def save_uploaded_image(
    workspace: Path,
    filename: str,
    data_base64: str,
    mime: str = "",
) -> dict[str, Any]:
    raw = data_base64.strip()
    if "," in raw and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=False)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "summary": "截图数据无法读取", "error_code": "invalid"}
    if not data:
        return {"ok": False, "summary": "截图是空的", "error_code": "empty"}
    if len(data) > _UPLOAD_MAX:
        return {"ok": False, "summary": "截图超过 2MB，请缩小后再贴。", "error_code": "too_large"}
    suffix = Path(filename).suffix.lower()
    if suffix not in _IMAGE_MIME:
        guessed = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
        }.get(mime.lower(), ".png")
        suffix = guessed
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(data).hexdigest()[:8]
    stem = _UPLOAD_NAME.sub("-", Path(filename).stem)[:40].strip("-") or "clip"
    relative = f".forge-uploads/{stamp}-{stem}-{digest}{suffix}"
    sandbox = WorkspaceSandbox(workspace)
    path = sandbox.resolve(relative, must_exist=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "ok": True,
        "summary": f"已保存截图 {relative}",
        "error_code": None,
        "path": relative,
    }


def image_data_urls(workspace: Path, relatives: list[str]) -> list[str]:
    sandbox = WorkspaceSandbox(workspace)
    urls: list[str] = []
    for relative in relatives[:4]:
        try:
            path = sandbox.resolve(relative, must_exist=True)
        except (ValueError, OSError, FileNotFoundError):
            continue
        if not path.is_file() or not is_image_path(path):
            continue
        if path.stat().st_size > _UPLOAD_MAX:
            continue
        mime = _IMAGE_MIME.get(path.suffix.lower(), "image/png")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        urls.append(f"data:{mime};base64,{encoded}")
    return urls


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
    raw_workspace = values.get("workspace")
    if isinstance(raw_workspace, str) and raw_workspace.strip():
        workspace = Path(raw_workspace).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"workspace is not a directory: {workspace}")
        updated["workspace"] = workspace.as_posix()
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
