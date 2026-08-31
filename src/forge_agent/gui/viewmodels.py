"""Pure event-to-view transformations used by the local frontend."""

from __future__ import annotations

import json
import re
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
        return TimelineItem(
            event.kind,
            _tool_title(name, payload, started=True),
            "进行中",
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
        if error == "interactive_command":
            return TimelineItem(
                event.kind,
                "交互程序请在右侧终端运行",
                summary
                or "单独输入 python 会打开交互环境。请改成 python 某个.py，或到右侧终端操作。",
                "warning",
                process=True,
            )
        if error == "hook_denied":
            metadata = payload.get("metadata")
            extra = metadata if isinstance(metadata, dict) else {}
            return TimelineItem(
                event.kind,
                _hook_denied_title(str(extra.get("hook_id") or payload.get("hook_id") or "")),
                _hook_denied_detail(
                    str(extra.get("pattern") or payload.get("pattern") or "").strip(),
                    str(extra.get("hook_id") or payload.get("hook_id") or ""),
                ),
                "danger",
                process=True,
            )
        if error == "timeout":
            arguments = payload.get("arguments")
            args = arguments if isinstance(arguments, dict) else {}
            target = _command_headline(str(args.get("command") or ""))
            return TimelineItem(
                event.kind,
                f"{target} 超时被停",
                summary or "超过限定时间后已停止。可以点「接着试」再跑一次。",
                "warning",
                process=True,
                path=_changed_path(payload),
            )
        detail = summary or f"{duration} 毫秒"
        if error and error not in detail:
            detail = f"{detail} · {error}"
        content = payload.get("content")
        diff = (
            str(content)
            if name
            in {
                "replace_in_file",
                "write_file",
                "delete_file",
                "undo_last_edit",
                "rollback_changes",
            }
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
                "user_rules_tokens",
                "memory_tokens",
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
    if event.kind == "memory_extracted":
        added = int(payload.get("added") or 0)
        return TimelineItem(
            event.kind,
            f"记下了 {added} 条记忆" if added else "这轮没有新的跨会话记忆",
            (
                "请到检查器「记忆」页确认。确认后的条目会在下次新会话自动带上。"
                if added
                else "抽取器认为没有值得跨会话保存的个人偏好、开发规范或踩过的坑。"
            ),
            "info",
            process=True,
        )
    if event.kind == "memory_extract_failed":
        return TimelineItem(
            event.kind,
            "记忆抽取没成功",
            "本轮对话结束后没能更新记忆文件。不影响这次改代码的结果。",
            "warning",
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
        labels = [
            _command_headline(str(command))
            for command in commands
            if str(command).strip()
        ]
        detail = f"将运行 {labels[0]}" if len(labels) == 1 else (
            f"将运行 {len(labels)} 项检查" if labels else "将按项目建议运行检查"
        )
        return TimelineItem(
            event.kind,
            "系统自动验证",
            detail,
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
    if event.kind == "judge_started":
        attempt = int(payload.get("attempt") or 1)
        return TimelineItem(
            event.kind,
            "评判器正在验收",
            f"第 {attempt} 次 · 对照原任务检查是否真的做完",
            "active",
            process=True,
        )
    if event.kind == "judge_finished":
        accepted = bool(payload.get("accepted"))
        missing = payload.get("missing")
        gaps = (
            "；".join(str(item) for item in missing if str(item).strip())
            if isinstance(missing, list)
            else ""
        )
        reason = str(payload.get("reason") or "").strip()
        detail = reason
        if not accepted and gaps:
            detail = f"{reason} 缺口：{gaps}" if reason else f"缺口：{gaps}"
        return TimelineItem(
            event.kind,
            "评判器通过" if accepted else "评判器认为还没做完",
            detail,
            "success" if accepted else "warning",
            process=True,
        )
    if event.kind == "hook_failed":
        return TimelineItem(
            event.kind,
            "Hook 没有成功执行",
            str(payload.get("error") or payload.get("reason") or "prompt hook 返回无法解析"),
            "warning",
            process=True,
        )
    if event.kind == "hook_skipped":
        return TimelineItem(
            event.kind,
            "已跳过 command hook",
            str(payload.get("reason") or "默认不执行任意脚本"),
            "info",
            process=True,
        )
    if event.kind == "hook_denied":
        return TimelineItem(
            event.kind,
            _hook_denied_title(str(payload.get("hook_id") or "")),
            _hook_denied_detail(
                str(payload.get("pattern") or "").strip(),
                str(payload.get("hook_id") or ""),
            ),
            "danger",
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
    if event.kind == "plan_ready":
        return TimelineItem(
            event.kind,
            "方案已提出，等待确认是否执行",
            str(payload.get("plan") or "")[:800],
            "warning",
            process=True,
        )
    if event.kind == "plan_approved":
        return TimelineItem(
            event.kind,
            "已确认，开始改代码",
            "",
            "success",
            process=True,
        )
    if event.kind == "approval_requested":
        if payload.get("kind") == "plan":
            return TimelineItem(
                event.kind,
                "等待你确认方案",
                "方案已在对话中给出。点「执行」才改代码。",
                "warning",
                process=True,
            )
        tool = str(payload.get("tool", "工具"))
        return TimelineItem(
            event.kind,
            _approval_title(tool, payload),
            _approval_reason_zh(str(payload.get("reason") or ""), tool),
            "warning",
            process=True,
        )
    if event.kind == "run_finished":
        status = str(payload.get("status", "finished"))
        steps = payload.get("steps")
        summary = str(payload.get("summary") or "")
        title, detail = _finish_text(status, summary, steps)
        return TimelineItem(
            event.kind,
            title,
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


def _hook_denied_title(hook_id: str) -> str:
    if hook_id == "block_secret_shell":
        return "已拦住读密钥或出沙箱"
    return "已拦住危险命令"


def _hook_denied_detail(pattern: str, hook_id: str) -> str:
    if hook_id == "block_secret_shell":
        if pattern:
            return f"匹配到 {pattern}。不能用命令读密钥或把文件拷出工作区。"
        return "不能用命令读密钥或把文件拷出工作区。"
    if pattern:
        return f"匹配到 {pattern}。这条命令不会执行，也不会再弹出确认。"
    return "这条命令不会执行，也不会再弹出确认。"


def _command_headline(command: str) -> str:
    """Turn a shell snippet into a short label, without dumping script bodies."""

    text = " ".join(command.strip().split())
    if not text:
        return "命令"
    lower = text.lower()
    if re.search(r"%[a-z]\.py\b", lower) or (
        re.search(r"\bfor\s+%[a-z]\b", lower) and "python" in lower
    ):
        return "工作区脚本"
    if re.search(r"(?:^|[;&|]\s*)(?:python(?:3)?|py)(?:\s+-\w+)*\s+-c\b", lower):
        return "Python 代码"
    if re.search(r"(?:^|[;&|]\s*)node\s+-e\b", lower):
        return "Node 代码"
    if re.search(r"powershell.*-(?:command|c)\b", lower) or re.search(
        r"(?:^|[;&|]\s*)pwsh(?:\.exe)?\s+-c\b", lower
    ):
        return "PowerShell 脚本"
    file_match = re.search(
        r"(?:python(?:3)?|py)\s+(?:-[XB]\s+)*(?!-m\b)([^\s]+\.py)\b",
        text,
        re.I,
    )
    if file_match:
        return Path(file_match.group(1).replace("\\", "/")).name
    if len(text) <= 48:
        return text
    return text[:45] + "…"


def _approval_title(tool: str, payload: dict[str, Any]) -> str:
    if tool in {"write_file", "replace_in_file", "delete_file"}:
        return "等待你允许写入文件"
    arguments = payload.get("arguments")
    args = arguments if isinstance(arguments, dict) else {}
    if tool == "run_command":
        return f"等待你批准：运行 {_command_headline(str(args.get('command') or ''))}"
    if tool == "verify_changes":
        return "等待你批准：运行验证"
    return "等待你批准这次操作"


def _approval_reason_zh(reason: str, tool: str) -> str:
    mapped = {
        "workspace content modification": "将改写工作区里的文件。允许后，本轮再写、改、删文件都不再询问。",
        "commands can cause side effects": "这条命令可能改文件或产生其它影响，需要你点头后才会执行。",
        "installing dependencies can change the workspace": "安装依赖会改动工作区，需要你点头后才会执行。",
        "network commands can cause side effects": "这条命令会访问网络，需要你点头后才会执行。",
    }
    if reason in mapped:
        return mapped[reason]
    if tool in {"write_file", "replace_in_file", "delete_file"}:
        return "将改写工作区里的文件。允许后，本轮再写、改、删文件都不再询问。"
    if reason.strip():
        return reason
    return "需要你确认后才会继续。"


def _tool_title(name: str, payload: dict[str, Any], *, started: bool, ok: bool = True) -> str:
    arguments = payload.get("arguments")
    args = arguments if isinstance(arguments, dict) else {}
    path = str(args.get("path") or _changed_path(payload) or "").replace("\\", "/")
    short = Path(path).name if path else ""
    command = _command_headline(str(args.get("command") or ""))
    query = str(args.get("query") or args.get("pattern") or "").strip()
    if len(query) > 24:
        query = query[:21] + "…"
    labels = {
        "read_file": f"读取 {short}" if short else "读取文件",
        "write_file": f"写入 {short}" if short else "写入文件",
        "delete_file": f"删除 {short}" if short else "删除文件",
        "replace_in_file": f"修改 {short}" if short else "修改文件",
        "list_files": "列出文件",
        "search_text": f"搜索 {query}" if query else "搜索代码",
        "run_command": f"运行 {command}" if command != "命令" else "运行命令",
        "verify_changes": f"验证 {command}" if command != "命令" else "运行验证",
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


def _finish_text(status: str, summary: str, steps: Any) -> tuple[str, str]:
    lower = summary.lower()
    step_bit = f"{steps} 步。 " if steps is not None else ""
    if "timeout" in lower or "timed out" in lower:
        return (
            "读模型超时",
            f"{step_bit}模型在限定时间内没有返回。点「接着试」会重发上一句，而不是另写一句继续。",
        )
    if status == "cancelled":
        return "已停止", f"{step_bit}已按你的操作停下。点「接着试」会重发上一句。"
    if status == "stopped":
        return "已中断", f"{step_bit}{summary or '任务中途停下。点「接着试」会重发上一句。'}"
    if status == "failed":
        return "这次没跑完", f"{step_bit}{summary or '运行失败。点「接着试」会重发上一句。'}"
    if status == "completed":
        return "运行已完成", step_bit.strip()
    return f"运行{_status_text(status)}", f"{step_bit}{summary}".strip()
