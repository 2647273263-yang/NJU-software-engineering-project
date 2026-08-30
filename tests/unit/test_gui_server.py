import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from forge_agent.gui.server import create_app
from forge_agent.model.fake import FakeModel
from forge_agent.types import ModelResponse, ToolCall


def tool_response(call_id: str, name: str, **arguments: Any) -> ModelResponse:
    return ModelResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


def test_meta_does_not_expose_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("FORGE_MODEL", "demo-model")
    app = create_app(database_path=tmp_path / "sessions.sqlite3")
    with TestClient(app) as client:
        payload = client.get("/api/meta").json()
    assert payload["has_api_key"] is True
    assert payload["model"] == "demo-model"
    assert "sk-should-not-leak" not in str(payload)


def test_index_serves_chat_shell(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "sessions.sqlite3")
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert 'id="root"' in response.text
    assert "ForgeAgent" in response.text


def test_start_run_requires_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_API_KEY", raising=False)
    monkeypatch.setenv("FORGE_MODEL", "fake")
    app = create_app(database_path=tmp_path / "sessions.sqlite3")
    with TestClient(app) as client:
        response = client.post(
            "/api/runs",
            json={"task": "do something", "workspace": str(tmp_path)},
        )
    assert response.status_code == 400
    assert "FORGE_API_KEY" in response.json()["detail"]


def test_api_run_records_diff_and_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")
    monkeypatch.setenv("FORGE_API_KEY", "test")
    monkeypatch.setenv("FORGE_MODEL", "fake")
    verify = 'python -B -c "from app import answer; assert answer() == 42"'
    model = FakeModel(
        [
            tool_response(
                "edit",
                "replace_in_file",
                path="app.py",
                old_text="return 41",
                new_text="return 42",
            ),
            tool_response("verify", "verify_changes", command=verify),
            ModelResponse(text="已修复并验证。"),
        ]
    )
    app = create_app(
        database_path=tmp_path / "sessions.sqlite3",
        model_factory=lambda _config: model,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/runs",
            json={
                "task": "Fix answer and verify it.",
                "workspace": str(tmp_path),
                "auto_approve": True,
                "demo": False,
            },
        )
        assert response.status_code == 200
        session_id = response.json()["session_id"]
        detail: dict[str, Any] | None = None
        deadline = time.time() + 15
        while time.time() < deadline:
            detail = client.get(f"/api/sessions/{session_id}?demo=false").json()
            if detail["session"]["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        assert detail is not None
        assert detail["session"]["status"] == "completed"
        views = [event["view"] for event in detail["events"]]
        assert any(view["kind"] == "user_message" for view in views)
        assert any(view.get("diff") and "+    return 42" in view["diff"] for view in views)
        assert any(view.get("answer") and "已修复" in view["detail"] for view in views)
        assert detail["claims"]
        first_claim = detail["claims"][0]
        assert first_claim["status"] in {"proven", "unproven", "unverifiable"}
        assert first_claim["items"]
        assert Path(detail["settings"]["workspace"]) == tmp_path
        tree = client.get("/api/workspace/tree", params={"workspace": str(tmp_path)}).json()
        assert any(node["name"] == "app.py" for node in tree["tree"])
        opened = client.get(
            "/api/workspace/file",
            params={"workspace": str(tmp_path), "path": "app.py", "session_id": session_id},
        ).json()
        assert opened["path"] == "app.py"
        assert "return 42" in opened["content"]


def test_gui_terminal_runs_echo(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "sessions.sqlite3")
    with TestClient(app) as client:
        payload = client.post(
            "/api/workspace/terminal",
            json={"workspace": str(tmp_path), "command": "echo hello"},
        ).json()
    assert payload["ok"] is True
    assert "hello" in payload["content"].lower()


def test_gui_terminal_cd_persists(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    app = create_app(database_path=tmp_path / "sessions.sqlite3")
    with TestClient(app) as client:
        changed = client.post(
            "/api/workspace/terminal",
            json={"workspace": str(tmp_path), "command": "cd sub"},
        ).json()
        assert changed["ok"] is True
        located = client.post(
            "/api/workspace/terminal",
            json={
                "workspace": str(tmp_path),
                "command": "(Get-Location).Path" if os.name == "nt" else "pwd",
            },
        ).json()
    assert located["ok"] is True
    assert "sub" in located["content"].replace("\\", "/").lower()


def test_gui_terminal_sessions_are_isolated(tmp_path: Path) -> None:
    from forge_agent.gui.shell import LiveTerminal, get_terminal

    shells: dict[str, LiveTerminal] = {}
    first = get_terminal(shells, tmp_path, "one")
    second = get_terminal(shells, tmp_path, "two")
    again = get_terminal(shells, tmp_path, "one")
    assert first is not second
    assert first is again
    assert len(shells) == 2


def test_gui_can_create_workspace_file(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "sessions.sqlite3")
    with TestClient(app) as client:
        created = client.put(
            "/api/workspace/file",
            json={
                "workspace": str(tmp_path),
                "path": "notes.md",
                "content": "hi\n",
                "create": True,
            },
        ).json()
        assert created["ok"] is True
        assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "hi\n"


def test_gui_save_file_overwrites_workspace_text(tmp_path: Path) -> None:
    target = tmp_path / "note.py"
    target.write_text("old\n", encoding="utf-8")
    app = create_app(database_path=tmp_path / "sessions.sqlite3")
    with TestClient(app) as client:
        payload = client.put(
            "/api/workspace/file",
            json={"workspace": str(tmp_path), "path": "note.py", "content": "print(1)\n"},
        ).json()
    assert payload["ok"] is True
    assert target.read_text(encoding="utf-8") == "print(1)\n"


def test_gui_can_delete_session(tmp_path: Path) -> None:
    from forge_agent.storage import SQLiteStorage

    database = tmp_path / "sessions.sqlite3"
    with SQLiteStorage(database) as storage:
        storage.create_session("sess-1", {"task": "demo", "workspace": str(tmp_path)})
    app = create_app(database_path=database)
    with TestClient(app) as client:
        assert client.delete("/api/sessions/sess-1").json()["deleted"] is True
        assert client.get("/api/sessions").json()["sessions"] == []


def test_gui_session_json_export_and_import(tmp_path: Path) -> None:
    from forge_agent.storage import SQLiteStorage
    from forge_agent.types import Message

    database = tmp_path / "sessions.sqlite3"
    with SQLiteStorage(database) as storage:
        storage.create_session("sess-export", {"task": "修登录", "workspace": str(tmp_path), "mode": "build"})
        storage.append_message("sess-export", Message(role="user", content="请修登录"))
        storage.append_event("sess-export", "user_message", {"text": "请修登录"})
        storage.append_event("sess-export", "model_response", {"text": "已看过代码", "tool_calls": 0})
        claim = storage.save_claim("sess-export", "登录可以跑", "verified")
        storage.save_evidence(claim.id, "test", "pytest 通过")
    app = create_app(database_path=database)
    with TestClient(app) as client:
        exported = client.get("/api/sessions/sess-export/export")
        assert exported.status_code == 200
        bundle = exported.json()
        assert bundle["format"] == "forge-agent.session"
        assert bundle["session"]["metadata"]["task"] == "修登录"
        assert len(bundle["messages"]) == 1
        imported = client.post("/api/sessions/import", json=bundle)
        assert imported.status_code == 200
        new_id = imported.json()["session_id"]
        assert new_id != "sess-export"
        detail = client.get(f"/api/sessions/{new_id}").json()
        assert detail["session"]["task"] == "修登录"
        assert any(event["view"]["kind"] == "user_message" for event in detail["events"])
        assert detail["claims"][0]["statement"] == "登录可以跑"


def test_accepted_diffs_persist_on_session(tmp_path: Path) -> None:
    from forge_agent.storage import SQLiteStorage

    database = tmp_path / "sessions.sqlite3"
    with SQLiteStorage(database) as storage:
        storage.create_session("sess-accept", {"task": "改排序", "workspace": str(tmp_path)})
    app = create_app(database_path=database)
    with TestClient(app) as client:
        empty = client.get("/api/sessions/sess-accept").json()
        assert empty["accepted_diffs"] == {}
        saved = client.patch(
            "/api/sessions/sess-accept/accepted-diffs",
            json={"diffs": {"bubble_sort.py": "--- a/bubble_sort.py\n+++ b/bubble_sort.py\n"}},
        )
        assert saved.status_code == 200
        assert "bubble_sort.py" in saved.json()["accepted_diffs"]
        reloaded = client.get("/api/sessions/sess-accept").json()
        assert reloaded["accepted_diffs"]["bubble_sort.py"].startswith("--- a/bubble_sort.py")
        fingerprint = "bubble_sort.py::" + "b" * 64
        saved = client.patch(
            "/api/sessions/sess-accept/accepted-diffs",
            json={"diffs": {"bubble_sort.py": fingerprint}},
        )
        assert saved.status_code == 200
        client.patch(
            "/api/sessions/sess-accept/settings",
            json={"title": "排序", "mode": "build"},
        )
        after_settings = client.get("/api/sessions/sess-accept").json()
        assert after_settings["accepted_diffs"]["bubble_sort.py"] == fingerprint
        assert after_settings["session"]["task"] == "排序"


def test_gui_can_rename_and_delete_workspace_paths(tmp_path: Path) -> None:
    source = tmp_path / "old.py"
    source.write_text("print(1)\n", encoding="utf-8")
    nested = tmp_path / "pkg"
    nested.mkdir()
    (nested / "mod.py").write_text("x = 1\n", encoding="utf-8")
    app = create_app(database_path=tmp_path / "sessions.sqlite3")
    with TestClient(app) as client:
        renamed = client.post(
            "/api/workspace/rename",
            json={"workspace": str(tmp_path), "path": "old.py", "to": "new.py"},
        ).json()
        assert renamed["ok"] is True
        assert (tmp_path / "new.py").is_file()
        assert not source.exists()
        deleted = client.post(
            "/api/workspace/delete",
            json={"workspace": str(tmp_path), "path": "pkg"},
        ).json()
        assert deleted["ok"] is True
        assert not nested.exists()


def test_gui_can_change_idle_session_workspace(tmp_path: Path) -> None:
    from forge_agent.storage import SQLiteStorage

    other = tmp_path / "other"
    other.mkdir()
    database = tmp_path / "sessions.sqlite3"
    with SQLiteStorage(database) as storage:
        storage.create_session(
            "sess-ws",
            {"task": "demo", "workspace": str(tmp_path), "mode": "build"},
        )
    app = create_app(database_path=database)
    with TestClient(app) as client:
        changed = client.patch(
            "/api/sessions/sess-ws/settings",
            json={"workspace": str(other)},
        )
        assert changed.status_code == 200
        assert Path(changed.json()["settings"]["workspace"]).resolve() == other.resolve()
        missing = tmp_path / "missing-dir"
        failed = client.patch(
            "/api/sessions/sess-ws/settings",
            json={"workspace": str(missing)},
        )
        assert failed.status_code == 400

