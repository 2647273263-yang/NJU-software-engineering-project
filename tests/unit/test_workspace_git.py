from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forge_agent.gitbin import resolve_git_executable
from forge_agent.gui.server import create_app
from forge_agent.gui.workspace_git import (
    GitWorkspaceError,
    _Run,
    _friendly_push_error,
    _retryable_push_error,
    git_commit,
    git_create_branch,
    git_init,
    git_push,
    git_restore,
    git_set_remote,
    git_snapshot,
    git_switch_branch,
)


pytestmark = pytest.mark.skipif(resolve_git_executable() is None, reason="Git executable is not available")


def test_git_checkpoint_branch_and_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "tester")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "tester@example.com")
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "app.py").write_text("v1\n", encoding="utf-8")

    started = git_init(workspace)
    assert started["repo"] is True
    first = git_commit(workspace, "能跑的第一版")
    assert len(first["commits"]) == 1
    sha = first["commits"][0]["sha"]

    branched = git_create_branch(workspace, "agent-try")
    assert branched["branch"] == "agent-try"
    nodes = {item["name"]: item for item in branched["branch_nodes"]}
    assert nodes["main"]["parent"] == ""
    assert nodes["main"]["depth"] == 0
    assert nodes["agent-try"]["parent"] == "main"
    assert nodes["agent-try"]["depth"] == 1
    back = git_switch_branch(workspace, "main")
    back_nodes = {item["name"]: item for item in back["branch_nodes"]}
    assert back["branch"] == "main"
    assert back_nodes["main"]["parent"] == ""
    assert back_nodes["main"]["depth"] == 0
    assert back_nodes["agent-try"]["parent"] == "main"
    git_switch_branch(workspace, "agent-try")
    (workspace / "app.py").write_text("broken\n", encoding="utf-8")
    (workspace / "extra.txt").write_text("junk\n", encoding="utf-8")
    git_commit(workspace, "智能体改坏了")
    after = git_snapshot(workspace)
    try_node = next(item for item in after["branch_nodes"] if item["name"] == "agent-try")
    assert try_node["ahead"] >= 1
    assert (workspace / "app.py").read_text(encoding="utf-8") == "broken\n"

    restored = git_restore(workspace, sha, confirm=True, clean_untracked=True)
    assert restored["branch"] == "agent-try"
    assert (workspace / "app.py").read_text(encoding="utf-8") == "v1\n"
    assert not (workspace / "extra.txt").exists()

    nested = git_create_branch(workspace, "try-2")
    nested_node = next(item for item in nested["branch_nodes"] if item["name"] == "try-2")
    assert nested_node["parent"] == "agent-try"
    assert nested_node["depth"] == 2


def test_gui_git_restore_requires_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "tester")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "tester@example.com")
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "app.py").write_text("ok\n", encoding="utf-8")
    app = create_app(database_path=tmp_path / "sessions.sqlite3")
    with TestClient(app) as client:
        assert client.post("/api/workspace/git/init", json={"workspace": str(workspace)}).status_code == 200
        saved = client.post(
            "/api/workspace/git/commit",
            json={"workspace": str(workspace), "message": "稳妥版本"},
        )
        assert saved.status_code == 200
        sha = saved.json()["commits"][0]["sha"]
        (workspace / "app.py").write_text("bad\n", encoding="utf-8")
        denied = client.post(
            "/api/workspace/git/restore",
            json={"workspace": str(workspace), "commit": sha, "confirm": False},
        )
        assert denied.status_code == 400
        ok = client.post(
            "/api/workspace/git/restore",
            json={"workspace": str(workspace), "commit": sha, "confirm": True},
        )
        assert ok.status_code == 200
        assert (workspace / "app.py").read_text(encoding="utf-8") == "ok\n"
        status = client.get("/api/workspace/git", params={"workspace": str(workspace)})
        assert status.json()["repo"] is True
        assert status.json()["commits"]


def test_git_github_remote_and_push_current_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "tester")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "tester@example.com")
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "app.py").write_text("ok\n", encoding="utf-8")
    git_init(workspace)
    git_commit(workspace, "第一版")
    with pytest.raises(GitWorkspaceError, match="github.com"):
        git_set_remote(workspace, "https://example.com/not-github.git")
    linked = git_set_remote(
        workspace,
        "https://github.com/2647273263-yang/NJU-software-engineering-project",
    )
    assert linked["remote_url"].endswith("NJU-software-engineering-project.git")

    git = resolve_git_executable()
    assert git is not None
    bare = tmp_path / "origin.git"
    subprocess.run([git, "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    subprocess.run(
        [git, "-C", str(workspace), "remote", "set-url", "origin", str(bare)],
        check=True,
        capture_output=True,
    )
    pushed = git_push(workspace)
    assert pushed["branch"] == "main"
    listed = subprocess.run(
        [git, "--git-dir", str(bare), "branch", "--list"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "main" in listed.stdout


def test_push_connection_reset_is_explained() -> None:
    result = _Run(
        128,
        "",
        "fatal: unable to access 'https://github.com/example/test.git/': Recv failure: Connection was reset",
    )
    assert _retryable_push_error(result)
    assert "连接被重置" in _friendly_push_error(result)
    assert "没有成功" in _friendly_push_error(result)
    auth = _Run(128, "", "remote: Invalid username or password\nAuthentication failed")
    assert not _retryable_push_error(auth)

