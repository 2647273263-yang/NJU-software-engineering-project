from pathlib import Path

import pytest

from forge_agent.application import ApplicationEvent
from forge_agent.gui import app as gui_app
from forge_agent.gui.viewmodels import event_to_view


def event(kind: str, payload: dict[str, object]) -> ApplicationEvent:
    return ApplicationEvent(
        session_id="session",
        kind=kind,
        payload=payload,
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_tool_titles_use_file_and_command_names() -> None:
    started = event_to_view(
        event(
            "tool_started",
            {"name": "replace_in_file", "arguments": {"path": "src/app.py"}},
        )
    )
    finished = event_to_view(
        event(
            "tool_finished",
            {
                "name": "run_command",
                "ok": True,
                "duration_ms": 20,
                "summary": "passed",
                "arguments": {"command": "pytest -q"},
            },
        )
    )
    assert started.title == "修改 app.py"
    assert finished.title == "运行 pytest -q 完成"
    view = event_to_view(
        event(
            "tool_finished",
            {
                "name": "replace_in_file",
                "ok": True,
                "duration_ms": 12,
                "content": "--- a/app.py\n+++ b/app.py\n-return 1\n+return 2",
            },
        )
    )

    assert view.tone == "success"
    assert view.diff is not None
    assert "+return 2" in view.diff


def test_delete_file_finished_event_exposes_diff() -> None:
    view = event_to_view(
        event(
            "tool_finished",
            {
                "name": "delete_file",
                "ok": True,
                "duration_ms": 4,
                "summary": "deleted gone.py",
                "content": "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-print(1)\n",
                "metadata": {"changed_files": ["gone.py"], "deleted": True},
                "arguments": {"path": "gone.py"},
            },
        )
    )
    assert view.title == "删除 gone.py 完成"
    assert view.diff is not None
    assert view.path == "gone.py"
    assert "+++ /dev/null" in view.diff


def test_demo_view_redacts_workspace_and_secret() -> None:
    workspace = Path("C:/Users/Example/private-project")  # forge-release: allow
    view = event_to_view(
        event(
            "tool_started",
            {
                "name": "run_command",
                "arguments": {
                    "command": (
                        "tool --workspace C:/Users/Example/private-project "  # forge-release: allow
                        "--api-key sk-abcdefghijklmnopqrstuvwxyz"  # forge-release: allow
                    )
                },
            },
        ),
        workspace=workspace,
    )

    assert "Example" not in view.title
    assert "abcdefghijklmnopqrstuvwxyz" not in view.title
    assert "abcdefghijklmnopqrstuvwxyz" not in view.detail
    assert "$WORKSPACE" in view.title or "[REDACTED_KEY]" in view.title
    assert view.detail == "进行中"


def test_run_command_titles_do_not_dump_script_bodies() -> None:
    started = event_to_view(
        event(
            "tool_started",
            {
                "name": "run_command",
                "arguments": {
                    "command": 'python -c "print(open(\'app.py\').read()); x = 1\\n" * 40'
                },
            },
        )
    )
    assert started.title == "运行 Python 代码"
    assert "print(" not in started.title
    assert "{" not in started.detail
    looped = event_to_view(
        event(
            "tool_started",
            {
                "name": "run_command",
                "arguments": {"command": r"for %f in (*.py) do python %f"},
            },
        )
    )
    assert looped.title == "运行 工作区脚本"


def test_approval_requested_is_plain_chinese() -> None:
    write = event_to_view(
        event(
            "approval_requested",
            {
                "tool": "write_file",
                "arguments": {"path": "app.py", "content": "print(1)\\n" * 20},
                "reason": "workspace content modification",
            },
        )
    )
    command = event_to_view(
        event(
            "approval_requested",
            {
                "tool": "run_command",
                "arguments": {"command": "pip install requests"},
                "reason": "installing dependencies can change the workspace",
            },
        )
    )
    assert write.title == "等待你允许写入文件"
    assert "print" not in write.detail
    assert command.title.startswith("等待你批准")
    assert "pip install" in command.title
    assert "改动工作区" in command.detail


def test_context_compaction_populates_dashboard_data() -> None:
    view = event_to_view(
        event(
            "context_compacted",
            {
                "messages_compacted": 8,
                "estimated_tokens_before": 10_000,
                "estimated_tokens_after": 4_000,
                "summary": {"goal": "fix tests"},
            },
        )
    )

    assert view.context == {
        "messages": 8,
        "before": 10_000,
        "after": 4_000,
        "summary": {"goal": "fix tests"},
    }


def test_hypothesis_and_workspace_summary_have_chinese_titles() -> None:
    hypothesis = event_to_view(
        event(
            "hypothesis_updated",
            {
                "retired": True,
                "observed_failure": "`pytest` exited 1",
            },
        )
    )
    summary = event_to_view(
        event(
            "workspace_summary",
            {
                "available": True,
                "changed_entries": ["app.py"],
                "insertions": 2,
                "deletions": 1,
                "untracked": 0,
            },
        )
    )

    assert hypothesis.title == "调试假设已用尽"
    assert "pytest" in hypothesis.detail
    assert summary.title == "工作树汇总"
    assert "+2/-1" in summary.detail


def test_final_model_text_is_an_answer_not_run_status() -> None:
    answer = event_to_view(
        event(
            "model_response",
            {
                "tokens": 80,
                "tool_calls": 0,
                "text": "# ForgeAgent 项目分析报告\n\n核心循环是 model → action。",
            },
        )
    )
    finished = event_to_view(
        event("run_finished", {"status": "completed", "steps": 4, "summary": "should not dump"})
    )
    git_missing = event_to_view(
        event(
            "tool_finished",
            {
                "name": "git_status",
                "ok": False,
                "duration_ms": 130,
                "error_code": "git_unavailable",
                "summary": "Git is not installed or not on PATH",
            },
        )
    )

    assert answer.answer is True
    assert "项目分析报告" in answer.detail
    assert finished.process is True
    assert "should not dump" not in finished.detail
    assert git_missing.title == "Git 不可用，已跳过"
    assert git_missing.process is True


def test_timeout_and_interactive_failures_are_plain_chinese() -> None:
    timeout = event_to_view(
        event(
            "tool_finished",
            {
                "name": "run_command",
                "ok": False,
                "error_code": "timeout",
                "summary": "command timed out",
                "arguments": {"command": "python -m pytest -q"},
            },
        )
    )
    interactive = event_to_view(
        event(
            "tool_finished",
            {
                "name": "run_command",
                "ok": False,
                "error_code": "interactive_command",
                "summary": "交互式程序不会在 Agent 里启动。",
            },
        )
    )
    model_timeout = event_to_view(
        event("run_finished", {"status": "failed", "steps": 2, "summary": "timeout: model request timed out"})
    )
    assert "超时" in timeout.title
    assert interactive.title.startswith("交互")
    assert model_timeout.title == "读模型超时"


def test_model_text_with_tool_calls_is_still_an_answer() -> None:
    view = event_to_view(
        event(
            "model_response",
            {
                "tokens": 40,
                "tool_calls": 2,
                "text": "接下来会读取配置并修改入口文件。",
            },
        )
    )
    assert view.answer is True
    assert "读取配置" in view.detail


def test_user_message_is_chat_text() -> None:
    view = event_to_view(event("user_message", {"text": "修复失败的测试"}))
    assert view.kind == "user_message"
    assert view.detail == "修复失败的测试"
    assert view.process is False
    assert view.answer is False


def test_plan_approval_events_use_chinese_titles() -> None:
    requested = event_to_view(
        event(
            "approval_requested",
            {
                "kind": "plan",
                "plan": "Goal: fix tests\nFeasibility: yes",
                "reason": "Confirm the plan",
            },
        )
    )
    ready = event_to_view(event("plan_ready", {"plan": "a plan"}))
    approved = event_to_view(event("plan_approved", {"mode": "build"}))

    assert requested.title == "等待你确认方案"
    assert "才改代码" in requested.detail
    assert ready.title.startswith("方案已提出")
    assert approved.title.startswith("已确认")


def test_gui_shutdown_by_keyboard_interrupt_is_graceful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(gui_app.uvicorn, "run", interrupt)

    gui_app.run_gui(show=False)
