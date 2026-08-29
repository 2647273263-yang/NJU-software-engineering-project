from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import pytest

from forge_agent.tools import WorkspaceSandbox, WorkspaceViolation, build_default_registry
from forge_agent.tools.command import CommandTools
from forge_agent.tools.schemas import RunCommandArgs
from forge_agent.tools.sensitive import sensitive_read_reason


def test_sandbox_rejects_parent_absolute_and_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    sandbox = WorkspaceSandbox(workspace)

    with pytest.raises(WorkspaceViolation):
        sandbox.resolve("../outside")
    with pytest.raises(WorkspaceViolation):
        sandbox.resolve(outside)

    link = workspace / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is not permitted on this system")
    with pytest.raises(WorkspaceViolation):
        sandbox.resolve("link/new.txt")


def test_sensitive_read_reason_blocks_secrets_but_allows_examples() -> None:
    assert sensitive_read_reason(".env") == ".env"
    assert sensitive_read_reason("config/.env.local") == ".env"
    assert sensitive_read_reason(".env.example") is None
    assert sensitive_read_reason(".git/config") == ".git internals"
    assert sensitive_read_reason(".ssh/id_rsa") == "credential directory"
    assert sensitive_read_reason("certs/server.pem") == "private key"
    assert sensitive_read_reason("src/app.py") is None


@pytest.mark.asyncio
async def test_read_file_blocks_dotenv_and_git_objects(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=supersecret\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("SECRET=\n", encoding="utf-8")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n", encoding="utf-8")
    registry = build_default_registry(tmp_path)

    blocked = await registry.call("read_file", {"path": ".env"})
    example = await registry.call("read_file", {"path": ".env.example"})
    git_object = await registry.call("read_file", {"path": ".git/config"})
    search = await registry.call("search_text", {"query": "supersecret"})

    assert not blocked.ok
    assert blocked.error_code == "SensitivePathError"
    assert "supersecret" not in (blocked.content or "")
    assert "supersecret" not in blocked.summary
    assert example.ok
    assert "SECRET=" in example.content
    assert not git_object.ok
    assert git_object.error_code == "SensitivePathError"
    assert search.ok
    assert "supersecret" not in search.content


@pytest.mark.asyncio
async def test_registry_validates_arguments_and_unknown_tools(tmp_path: Path) -> None:
    registry = build_default_registry(tmp_path)
    invalid = await registry.call("read_file", {"path": "x", "extra": True})
    unknown = await registry.call("missing", {})
    assert invalid.error_code == "invalid_arguments"
    assert unknown.error_code == "unknown_tool"
    assert {schema["function"]["name"] for schema in registry.schemas()} >= {
        "read_file",
        "run_command",
    }


@pytest.mark.asyncio
async def test_registry_reads_two_files_concurrently(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("bravo", encoding="utf-8")
    registry = build_default_registry(tmp_path)

    first, second = await asyncio.gather(
        registry.call("read_file", {"path": "a.txt"}),
        registry.call("read_file", {"path": "b.txt"}),
    )

    assert first.ok
    assert second.ok
    assert "alpha" in first.content
    assert "bravo" in second.content


@pytest.mark.asyncio
async def test_file_tools_hash_unique_replace_and_atomic_write(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("one two one", encoding="utf-8")
    registry = build_default_registry(tmp_path)

    ambiguous = await registry.call(
        "replace_in_file",
        {"path": "sample.txt", "old_text": "one", "new_text": "three"},
    )
    assert not ambiguous.ok
    assert target.read_text(encoding="utf-8") == "one two one"

    wrong_hash = await registry.call(
        "write_file",
        {"path": "sample.txt", "content": "changed", "expected_sha256": "0" * 64},
    )
    assert not wrong_hash.ok
    assert target.read_text(encoding="utf-8") == "one two one"

    expected = hashlib.sha256(b"one two one").hexdigest()
    written = await registry.call(
        "write_file",
        {"path": "sample.txt", "content": "changed", "expected_sha256": expected},
    )
    assert written.ok
    assert "--- a/sample.txt" in written.content
    assert written.metadata["sha256"] == hashlib.sha256(b"changed").hexdigest()
    assert target.read_text(encoding="utf-8") == "changed"
    assert not list(tmp_path.glob(".sample.txt.*"))

    undone = await registry.call("undo_last_edit", {})
    assert undone.ok
    assert undone.metadata["undo"] is True
    assert target.read_text(encoding="utf-8") == "one two one"


@pytest.mark.asyncio
async def test_write_file_create_and_delete_file_emit_whole_file_diffs(tmp_path: Path) -> None:
    registry = build_default_registry(tmp_path)

    created = await registry.call("write_file", {"path": "new.txt", "content": "hello\n"})
    assert created.ok
    assert created.metadata["created"] is True
    assert created.content.startswith("--- /dev/null")
    assert "+++ b/new.txt" in created.content
    assert "+hello" in created.content
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello\n"

    empty = await registry.call("write_file", {"path": "empty.txt", "content": ""})
    assert empty.ok
    assert empty.metadata["created"] is True
    assert empty.content.startswith("--- /dev/null")
    assert "+++ b/empty.txt" in empty.content
    assert (tmp_path / "empty.txt").is_file()

    deleted = await registry.call("delete_file", {"path": "new.txt"})
    assert deleted.ok
    assert deleted.metadata["deleted"] is True
    assert "--- a/new.txt" in deleted.content
    assert "+++ /dev/null" in deleted.content
    assert "-hello" in deleted.content
    assert not (tmp_path / "new.txt").exists()

    restored = await registry.call("undo_last_edit", {})
    assert restored.ok
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello\n"


@pytest.mark.asyncio
async def test_replace_in_file_honors_expected_replacements(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("one two one", encoding="utf-8")
    registry = build_default_registry(tmp_path)

    wrong_count = await registry.call(
        "replace_in_file",
        {
            "path": "sample.txt",
            "old_text": "one",
            "new_text": "three",
            "expected_replacements": 3,
        },
    )
    assert not wrong_count.ok
    assert target.read_text(encoding="utf-8") == "one two one"

    replaced = await registry.call(
        "replace_in_file",
        {
            "path": "sample.txt",
            "old_text": "one",
            "new_text": "three",
            "expected_replacements": 2,
        },
    )
    assert replaced.ok
    assert replaced.metadata["replacements"] == 2
    assert target.read_text(encoding="utf-8") == "three two three"


@pytest.mark.asyncio
async def test_read_list_and_search_are_bounded(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("Alpha\nneedle here\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle again\n", encoding="utf-8")
    registry = build_default_registry(tmp_path, max_output_chars=1_000)

    result = await registry.call(
        "search_text",
        {"query": "NEEDLE", "case_sensitive": False, "glob": "*.txt", "max_matches": 1},
    )
    assert result.ok
    assert result.truncated
    assert result.metadata["count"] == 1
    listing = await registry.call("list_files", {"recursive": True})
    assert "a.txt" in listing.content


@pytest.mark.asyncio
async def test_run_command_exit_timeout_and_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_root = tmp_path.parent / "app-data"
    monkeypatch.setattr(
        "forge_agent.tools.command.user_data_path",
        lambda *_args, **_kwargs: log_root,
    )
    registry = build_default_registry(tmp_path, command_timeout_s=0.1, max_output_chars=40)
    quoted_python = f'"{sys.executable}"'

    output = await registry.call(
        "run_command",
        {
            "command": (
                f'{quoted_python} -c "import sys; '
                "print('HEAD-' + 'x' * 100 + '-TAIL'); "
                "sys.stderr.write('STDERR-END')\""
            ),
            "timeout_s": 2,
        },
    )
    assert output.ok
    assert output.truncated
    assert len(output.content) <= 40
    assert output.content.startswith("HEAD-")
    assert output.content.endswith("STDERR-END")
    assert "omitted" in output.content
    assert "x" * 50 not in output.content
    log_path = Path(output.metadata["log_path"])
    assert log_path.exists()
    assert not log_path.is_relative_to(tmp_path)
    full_log = log_path.read_text(encoding="utf-8")
    assert "HEAD-" + "x" * 100 + "-TAIL" in full_log
    assert "STDERR-END" in full_log

    failure = await registry.call(
        "run_command",
        {"command": f'{quoted_python} -c "raise SystemExit(7)"', "timeout_s": 2},
    )
    assert failure.metadata["exit_code"] == 7
    assert failure.error_code == "command_failed"

    sleep_code = "import time; time.sleep(2)"
    timeout = await registry.call(
        "run_command",
        {"command": f'{quoted_python} -c "{sleep_code}"', "timeout_s": 0.05},
    )
    assert timeout.error_code == "timeout"
    assert timeout.metadata["timed_out"] is True


@pytest.mark.asyncio
async def test_run_command_rejects_tty_programs_and_allows_output_callback(
    tmp_path: Path,
) -> None:
    chunks: list[tuple[str, str]] = []

    async def on_output(stream: str, text: str) -> None:
        chunks.append((stream, text))

    commands = CommandTools(WorkspaceSandbox(tmp_path), on_output=on_output)
    python = f'"{sys.executable}"'

    refused = await commands.run_command(RunCommandArgs(command=python))
    assert not refused.ok
    assert refused.error_code == "interactive_command"
    assert refused.metadata["interactive"] is True

    completed = await commands.run_command(
        RunCommandArgs(
            command=(
                f'{python} -c "import sys; '
                "print('out password=supersecret'); sys.stderr.write('err')\""
            )
        )
    )
    assert completed.ok
    assert "supersecret" not in completed.content
    assert "supersecret" not in completed.metadata["command"]
    assert any(
        stream == "stdout" and text.strip() == "out password=[REDACTED]"
        for stream, text in chunks
    )
    assert ("stderr", "err") in chunks


@pytest.mark.asyncio
async def test_git_diff_reports_changes(tmp_path: Path) -> None:
    if os.system("git --version >NUL 2>&1" if os.name == "nt" else "git --version >/dev/null 2>&1"):
        pytest.skip("git is unavailable")
    registry = build_default_registry(tmp_path)
    os.system(f'git -C "{tmp_path}" init -q')
    (tmp_path / "tracked.txt").write_text("new\n", encoding="utf-8")
    os.system(f'git -C "{tmp_path}" add tracked.txt')
    result = await registry.call("git_diff", {"staged": True})
    assert result.ok
    assert "tracked.txt" in result.content


@pytest.mark.asyncio
async def test_git_status_reports_untracked_and_handles_missing_repo(tmp_path: Path) -> None:
    registry = build_default_registry(tmp_path)
    missing = await registry.call("git_status", {})
    assert not missing.ok
    assert missing.error_code in {"git_failed", "git_unavailable"}

    if os.system("git --version >NUL 2>&1" if os.name == "nt" else "git --version >/dev/null 2>&1"):
        return
    os.system(f'git -C "{tmp_path}" init -q')
    (tmp_path / "new.txt").write_text("hello\n", encoding="utf-8")
    status = await registry.call("git_status", {})
    assert status.ok
    assert "new.txt" in status.content
    assert status.metadata["untracked"] >= 1


@pytest.mark.asyncio
async def test_run_command_rejects_background_services(tmp_path: Path) -> None:
    commands = CommandTools(WorkspaceSandbox(tmp_path))
    refused = await commands.run_command(RunCommandArgs(command="npm start"))
    assert not refused.ok
    assert refused.error_code == "background_service"
    assert refused.metadata["background"] is True


@pytest.mark.asyncio
async def test_verify_changes_uses_suggested_command_when_omitted(tmp_path: Path) -> None:
    command = f'"{sys.executable}" -c "print(1)"'
    registry = build_default_registry(tmp_path, suggested_verification=[command])
    result = await registry.call("verify_changes", {})
    assert result.ok
    assert result.metadata["is_verification"] is True
    assert result.metadata["verification"]["command"] == command
