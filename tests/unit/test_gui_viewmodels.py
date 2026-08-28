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

    assert "Example" not in view.detail
    assert "abcdefghijklmnopqrstuvwxyz" not in view.detail
    assert "$WORKSPACE" in view.detail


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


def test_gui_shutdown_by_keyboard_interrupt_is_graceful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(gui_app.uvicorn, "run", interrupt)

    gui_app.run_gui(show=False)
