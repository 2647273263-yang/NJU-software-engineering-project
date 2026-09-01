from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from forge_agent.context.memory import (
    MemoryItem,
    append_memories,
    load_memories,
    memory_auto_extract,
    render_retrieved_memory,
    retrieve_memories,
    sanitize_candidate,
    set_memory_auto_extract,
    update_memory,
)
from forge_agent.context.project import ProjectContext


def _item(**overrides: object) -> MemoryItem:
    base = {
        "id": "m1",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "kind": "convention",
        "text": "Use pytest, not unittest.",
        "tags": ["pytest", "python"],
        "evidence": "session",
        "status": "proposed",
    }
    base.update(overrides)
    item = MemoryItem.from_dict(base)
    assert item is not None
    return item


def test_sanitize_drops_secrets_and_absolute_paths(tmp_path: Path) -> None:
    secret = sanitize_candidate(  # forge-release: allow
        {
            "kind": "fact",
            "text": "API key is sk-abcdefghijklmnopqrstuvwxyz",  # forge-release: allow
            "tags": [],
        },
        workspace=tmp_path,
    )
    path = sanitize_candidate(
        {
            "kind": "fact",
            "text": r"The repo lives at C:\Users\someone\project",  # forge-release: allow
            "tags": [],
        },
        workspace=tmp_path,
    )
    ok = sanitize_candidate(
        {
            "kind": "preference",
            "text": "Reply in Chinese.",
            "tags": ["language"],
        },
        workspace=tmp_path,
    )
    inventory = sanitize_candidate(
        {
            "kind": "fact",
            "text": "The repo has six sort algorithms and sorting.py as the entry.",
            "tags": ["sorting"],
        },
        workspace=tmp_path,
    )
    assert secret is None
    assert path is None
    assert ok is not None
    assert ok.status == "proposed"
    assert ok.text == "Reply in Chinese."
    assert inventory is None


def test_append_skips_near_duplicates(tmp_path: Path) -> None:
    first = append_memories(tmp_path, [_item(id="a")])
    again = append_memories(
        tmp_path,
        [_item(id="b", text="Use pytest, not unittest.")],
    )
    assert len(first) == 1
    assert again == []
    assert len(load_memories(tmp_path)) == 1


def test_retrieve_prefers_recent_tagged_conventions(tmp_path: Path) -> None:
    old = datetime.now(timezone.utc) - timedelta(days=40)
    append_memories(
        tmp_path,
        [
            _item(id="old", created_at=old.isoformat(), text="Ancient note.", tags=["pytest"]),
            _item(
                id="pref",
                kind="preference",
                text="Use tabs.",
                tags=["style"],
                status="proposed",
            ),
            _item(
                id="hit",
                text="Run pytest -q after edits.",
                tags=["pytest"],
                status="proposed",
            ),
            _item(
                id="accepted-pref",
                kind="preference",
                text="Reply in Chinese.",
                tags=["language"],
                status="accepted",
            ),
            _item(
                id="inventory",
                kind="fact",
                text="sorting.py is the unified entry with six algorithms.",
                tags=["pytest", "python"],
                status="accepted",
            ),
        ],
    )
    found = retrieve_memories(
        tmp_path,
        task="fix failing pytest",
        project=ProjectContext(project_type="Python", detected_files=("pyproject.toml",)),
    )
    texts = [item.text for item in found]
    assert "Ancient note." not in texts
    assert "Use tabs." not in texts
    assert "Run pytest -q after edits." in texts
    assert "Reply in Chinese." in texts
    assert "sorting.py is the unified entry with six algorithms." not in texts
    rendered = render_retrieved_memory(found)
    assert rendered is not None
    assert rendered.startswith("[retrieved memory]")
    assert "not a new instruction" in rendered.lower() or "not new instructions" in rendered.lower()


def test_auto_extract_setting_roundtrip(tmp_path: Path) -> None:
    assert memory_auto_extract(tmp_path) is True
    set_memory_auto_extract(tmp_path, False)
    assert memory_auto_extract(tmp_path) is False
    updated = update_memory(
        tmp_path,
        append_memories(tmp_path, [_item(id="x")])[0].id,
        status="accepted",
    )
    assert updated is not None
    assert updated.status == "accepted"


@pytest.mark.asyncio
async def test_runtime_injects_retrieved_memory(tmp_path: Path) -> None:
    from forge_agent.context.budget import ContextBudget
    from forge_agent.context.runtime import RuntimeContext
    from forge_agent.model.fake import FakeModel
    from forge_agent.types import Message

    append_memories(tmp_path, [_item(id="hit", status="accepted")])
    rendered = render_retrieved_memory(retrieve_memories(tmp_path, task="pytest"))
    events: list[tuple[str, dict[str, object]]] = []
    runtime = RuntimeContext(
        budget=ContextBudget(context_window=8_000, reserved_output_tokens=1_000),
        model=FakeModel([]),
        retrieved_memory=rendered,
        on_event=lambda kind, payload: events.append((kind, payload)),
    )
    prepared = await runtime.prepare(
        [Message(role="system", content="core"), Message(role="user", content="run tests")],
        [],
    )
    assert prepared[1].content is not None
    assert prepared[1].content.startswith("[retrieved memory]")
    assert "Use pytest" in (prepared[1].content or "")
    assert prepared[2].content == "run tests"
    payload = next(item for kind, item in events if kind == "context_prepared")
    assert int(payload["memory_tokens"]) > 0
