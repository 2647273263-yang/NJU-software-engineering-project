import pytest

from forge_agent.agent.tool_runtime import PersistentToolRuntime
from forge_agent.safety import PolicyEngine, PolicyToolRuntime
from forge_agent.storage import SQLiteStorage
from forge_agent.tools import build_default_registry
from forge_agent.types import RunMode, ToolCall


def runtime(workspace, storage) -> PersistentToolRuntime:
    return PersistentToolRuntime(
        PolicyToolRuntime(
            build_default_registry(workspace),
            PolicyEngine(mode=RunMode.BUILD, auto_approve=True),
        ),
        storage,
        "session-1",
        workspace,
    )


@pytest.mark.asyncio
async def test_undo_works_after_tool_runtime_restart(tmp_path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    with SQLiteStorage(tmp_path / "state.sqlite3") as storage:
        storage.create_session("session-1")
        first_runtime = runtime(tmp_path, storage)
        edited = await first_runtime.execute(
            ToolCall(
                id="edit",
                name="replace_in_file",
                arguments={
                    "path": "app.py",
                    "old_text": "value = 1",
                    "new_text": "value = 2",
                },
            )
        )
        assert edited.ok
        assert target.read_text(encoding="utf-8") == "value = 2\n"

        restarted_runtime = runtime(tmp_path, storage)
        undone = await restarted_runtime.execute(
            ToolCall(id="undo", name="undo_last_edit", arguments={})
        )

        assert undone.ok
        assert target.read_text(encoding="utf-8") == "value = 1\n"
        transactions = storage.list_edit_transactions("session-1")
        assert len(transactions) == 2
        assert transactions[-1].metadata["rolls_back"] == transactions[0].id


@pytest.mark.asyncio
async def test_group_rollback_restores_multiple_files_after_restart(tmp_path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("value = 1\n", encoding="utf-8")
    second.write_text("value = 2\n", encoding="utf-8")
    with SQLiteStorage(tmp_path / "state.sqlite3") as storage:
        storage.create_session("session-1")
        initial = runtime(tmp_path, storage)
        for call_id, path, old, new in (
            ("first", "first.py", "value = 1", "value = 10"),
            ("second", "second.py", "value = 2", "value = 20"),
        ):
            result = await initial.execute(
                ToolCall(
                    id=call_id,
                    name="replace_in_file",
                    arguments={"path": path, "old_text": old, "new_text": new},
                )
            )
            assert result.ok

        restarted = runtime(tmp_path, storage)
        rolled_back = await restarted.execute(
            ToolCall(id="rollback", name="rollback_changes", arguments={})
        )

        assert rolled_back.ok
        assert first.read_text(encoding="utf-8") == "value = 1\n"
        assert second.read_text(encoding="utf-8") == "value = 2\n"
        assert set(rolled_back.metadata["changed_files"]) == {"first.py", "second.py"}
