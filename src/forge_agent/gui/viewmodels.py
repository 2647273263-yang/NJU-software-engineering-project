"""Pure event-to-view transformations used by the local frontend."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from forge_agent.application import ApplicationEvent
from forge_agent.privacy import redact_data


@dataclass(frozen=True, slots=True)
class TimelineItem:
    kind: str
    title: str
    detail: str
    tone: str = "neutral"
    diff: str | None = None
    context: dict[str, Any] | None = None
    process: bool = False
    answer: bool = False
    path: str | None = None


def view_to_dict(item: TimelineItem) -> dict[str, Any]:
    return asdict(item)


def event_to_view(
    event: ApplicationEvent,
    *,
    workspace: Path | None = None,
) -> TimelineItem:
    payload = redact_data(event.payload, workspace=workspace)
    if event.kind == "user_message":
        return TimelineItem(event.kind, "用户", str(payload.get("text") or ""))
    if event.kind == "model_delta":
        return TimelineItem(
            event.kind,
            "流式输出",
            str(payload.get("text") or ""),
            "info",
        )
    if event.kind == "tool_started":
        name = str(payload.get("name", "tool"))
        arguments = payload.get("arguments", {})
        args_text = json.dumps(arguments, ensure_ascii=False)
        return TimelineItem(
            event.kind,
            _tool_title(name, payload, started=True),
            args_text,
            "active",
            process=True,
            path=_changed_path(payload),
        )
    if event.kind == "tool_finished":
        name = str(payload.get("name", "tool"))
        ok = bool(payload.get("ok"))
        duration = int(payload.get("duration_ms", 0))
        error = payload.get("error_code")
        summary = str(payload.get("summary") or "")
        if error in {"git_unavailable", "git_failed"} and name in {
            "git_status",
            "git_diff",
        }:
            return TimelineItem(
                event.kind,
                "Git 不可用，已跳过",
                summary or "本机未找到 git 命令",
                "warning",
                process=True,
            )
        detail = summary or f"{duration} 毫秒"
        if error and error not in detail:
            detail = f"{detail} · {error}"
        content = payload.get("content")
        diff = (
            str(content)
            if name in {"replace_in_file", "write_file", "undo_last_edit", "rollback_changes"}
            and content
            else None
        )
        return TimelineItem(
            event.kind,
            _tool_title(name, payload, started=False, ok=ok),
            detail,
            "success" if ok else "danger",
            diff=diff,
            process=True,
            path=_changed_path(payload),
        )
    if event.kind == "model_response":
        text = str(payload.get("text") or "").strip()
        tool_calls = int(payload.get("tool_calls") or 0)
        if text:
            return TimelineItem(
                event.kind,
                "回答",
                text,
                "success",
                answer=True,
            )
        return TimelineItem(
            event.kind,
            "模型响应",
            f"{payload.get('tokens', 0)} Token · {tool_calls} 个工具调用",
            "info",
            process=True,
        )
    if event.kind == "context_compacted":
        context = {
            "messages": payload.get("messages_compacted", 0),
            "before": payload.get("estimated_tokens_before", 0),
            "after": payload.get("estimated_tokens_after", 0),
            "summary": payload.get("summary", {}),
        }
        return TimelineItem(
            event.kind,
            "上下文已压缩",
            f"估算 Token：{context['before']} → {context['after']}",
            "warning",
            context=context,
            process=True,
        )
    if event.kind == "context_prepared":
        context = {
            key: payload.get(key, 0)
            for key in (
                "system_tokens",
                "project_tokens",
                "summary_tokens",
                "recent_tokens",
                "tool_schema_tokens",
                "total_tokens",
                "input_limit",
                "truncated_tool_outputs",
            )
        }
        return TimelineItem(
            event.kind,
            "上下文已组装",
            (f"估算 Token：{context['total_tokens']} / {context['input_limit']}"),
            "info",
            context=context,
            process=True,
        )
    if event.kind == "hypothesis_updated":
        retired = bool(payload.get("retired"))
        return TimelineItem(
            event.kind,
            "调试假设已用尽" if retired else "调试假设",
            str(payload.get("observed_failure") or payload.get("hypothesis", "")),
            "warning",
            process=True,
        )
    if event.kind == "automatic_verification_started":
        commands = payload.get("commands", [])
        return TimelineItem(
            event.kind,
            "系统自动验证",
            json.dumps(commands, ensure_ascii=False),
            "info",
            process=True,
        )
    if event.kind == "automatic_verification_finished":
        return TimelineItem(
            event.kind,
            "自动验证完成",
            "已通过" if payload.get("passed") else "未通过",
            "success" if payload.get("passed") else "warning",
            process=True,
        )
    if event.kind == "workspace_summary":
        if payload.get("available"):
            detail = (
                f"{len(payload.get('changed_entries', []) or [])} 个变更 · "
                f"+{payload.get('insertions', 0)}/"
                f"-{payload.get('deletions', 0)} · "
                f"{payload.get('untracked', 0)} 个未跟踪"
            )
        else:
            detail = str(payload.get("summary", "Git 不可用"))
        return TimelineItem(event.kind, "工作树汇总", detail, "info", process=True)
    if event.kind == "approval_requested":
        return TimelineItem(
            event.kind,
            f"需要审批：{payload.get('tool', '工具')}",
            str(payload.get("reason", "")),
            "warning",
            process=True,
        )
    if event.kind == "run_finished":
        status = str(payload.get("status", "finished"))
        steps = payload.get("steps")
        detail = f"{steps} 步" if steps is not None else ""
        return TimelineItem(
            event.kind,
            f"运行{_status_text(status)}",
            detail,
            "success" if status == "completed" else "warning",
            process=True,
        )
    return TimelineItem(
        event.kind,
        event.kind.replace("_", " "),
        json.dumps(payload, ensure_ascii=False)[:500],
        process=True,
    )


def _tool_title(name: str, payload: dict[str, Any], *, started: bool, ok: bool = True) -> str:
    arguments = payload.get("arguments")
    args = arguments if isinstance(arguments, dict) else {}
    path = str(args.get("path") or _changed_path(payload) or "").replace("\\", "/")
    short = Path(path).name if path else ""
    command = str(args.get("command") or "").strip().replace("\n", " ")
    if len(command) > 48:
        command = command[:45] + "…"
    query = str(args.get("query") or args.get("pattern") or "").strip()
    labels = {
        "read_file": f"读取 {short}" if short else "读取文件",
        "write_file": f"写入 {short}" if short else "写入文件",
        "replace_in_file": f"修改 {short}" if short else "修改文件",
        "list_files": "列出文件",
        "search_text": f"搜索 {query}" if query else "搜索代码",
        "run_command": f"运行 {command}" if command else "运行命令",
        "verify_changes": f"验证 {command}" if command else "运行验证",
        "git_status": "查看 Git 状态",
        "git_diff": "查看 Git 差异",
        "undo_last_edit": "撤销编辑",
        "rollback_changes": "回滚本轮修改",
        "repo_outline": "查看仓库结构",
    }
    base = labels.get(name, name.replace("_", " "))
    if started:
        return base
    return f"{base}{' 完成' if ok else ' 失败'}"


def _changed_path(payload: dict[str, Any]) -> str | None:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        changed = metadata.get("changed_files")
        if isinstance(changed, list) and changed and isinstance(changed[0], str):
            return changed[0]
    arguments = payload.get("arguments")
    if isinstance(arguments, dict):
        raw = arguments.get("path")
        if isinstance(raw, str) and raw.strip():
            return raw.replace("\\", "/")
    return None


def _status_text(status: str) -> str:
    return {
        "completed": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
        "stopped": "已停止",
    }.get(status, status)
