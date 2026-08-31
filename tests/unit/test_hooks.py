import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from forge_agent.agent.llm_judge import is_complex_coding_stop, parse_judge_response
from forge_agent.agent.loop import AgentLoop
from forge_agent.agent.state import AgentState
from forge_agent.config import RunConfig
from forge_agent.hooks.exfil import match_secret_or_escape_command
from forge_agent.hooks.spec import (
    HookConfig,
    HookEvent,
    HookType,
    before_tool_hooks,
    load_hook_config,
    stop_hooks,
)
from forge_agent.model.fake import FakeModel
from forge_agent.types import AgentStatus, ModelResponse, ToolCall, ToolResult


class FakeTools:
    def __init__(self, results: list[ToolResult]) -> None:
        self.results = results
        self.calls: list[ToolCall] = []

    def schemas(self) -> list[dict]:
        return []

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        return self.results[len(self.calls) - 1]


def _config(workspace: Path) -> RunConfig:
    return RunConfig(
        workspace=workspace,
        model="fake",
        api_key=SecretStr("test"),
        max_steps=12,
        max_model_calls=12,
    )


def _edit_then_verify() -> list[ToolResult]:
    return [
        ToolResult(
            ok=True,
            summary="edited",
            metadata={"changed_files": ["heap_sort.py"]},
        ),
        ToolResult(
            ok=True,
            summary="verified",
            metadata={
                "verification": {
                    "command": "pytest",
                    "exit_code": 0,
                    "duration_ms": 10,
                    "output": "1 passed",
                }
            },
        ),
    ]


def test_parse_judge_response_requires_boolean_accepted() -> None:
    ok = parse_judge_response(
        '```json\n{"accepted": true, "reason": "All requested files exist.", "missing": []}\n```'
    )
    bad = parse_judge_response("I think this is fine.")
    assert ok.accepted
    assert ok.missing == []
    assert not bad.accepted
    assert bad.parse_error


def test_complex_stop_requires_this_run_edits() -> None:
    state = AgentState()
    state.steps = 8
    assert not is_complex_coding_stop("实现堆排序和测试", state)
    state.run_changed_files.add("heap_sort.py")
    assert is_complex_coding_stop("实现堆排序和测试", state)
    assert not is_complex_coding_stop("hi", AgentState())


def test_load_hooks_json_disables_builtin_judge(tmp_path: Path) -> None:
    forge = tmp_path / ".forge"
    forge.mkdir()
    (forge / "hooks.json").write_text(
        json.dumps({"llm_judge": False}),
        encoding="utf-8",
    )
    config = load_hook_config(tmp_path)
    assert config.llm_judge is False
    assert stop_hooks(config) == []


@pytest.mark.asyncio
async def test_llm_judge_does_not_run_for_read_only_answer(tmp_path: Path) -> None:
    events: list[str] = []
    result = await AgentLoop(
        config=_config(tmp_path),
        model=FakeModel([ModelResponse(text="Bubble sort is stable.")]),
        tools=FakeTools([]),
        on_event=lambda kind, _payload: events.append(kind),
    ).run("分析每一个排序的优点")

    assert result.status is AgentStatus.COMPLETED
    assert "judge_started" not in events


@pytest.mark.asyncio
async def test_completion_judge_runs_before_llm_judge(tmp_path: Path) -> None:
    events: list[str] = []
    model = FakeModel(
        [
            ModelResponse(tool_calls=[ToolCall(id="1", name="fake")]),
            ModelResponse(text="Done without tests"),
            ModelResponse(tool_calls=[ToolCall(id="2", name="verify_changes")]),
            ModelResponse(text="Done with tests"),
        ]
    )
    result = await AgentLoop(
        config=_config(tmp_path),
        model=model,
        tools=FakeTools(_edit_then_verify()),
        on_event=lambda kind, _payload: events.append(kind),
    ).run("实现 heap_sort 并补测试")

    assert result.status is AgentStatus.COMPLETED
    assert events.index("verification_required") < events.index("judge_started")
    assert model.judge_calls
    assert len(model.calls) == 4


@pytest.mark.asyncio
async def test_prompt_stop_hook_blocks_early_completion(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    model = FakeModel(
        [
            ModelResponse(tool_calls=[ToolCall(id="1", name="fake")]),
            ModelResponse(tool_calls=[ToolCall(id="2", name="verify_changes")]),
            ModelResponse(text="Heap sort is done."),
            ModelResponse(tool_calls=[ToolCall(id="3", name="fake")]),
            ModelResponse(tool_calls=[ToolCall(id="4", name="verify_changes")]),
            ModelResponse(text="Quicksort and tests are also done."),
        ],
        judge_steps=[
            ModelResponse(
                text=(
                    '{"accepted": false, "reason": "quicksort is missing",'
                    ' "missing": ["quicksort.py"]}'
                )
            ),
            ModelResponse(
                text='{"accepted": true, "reason": "requested sorts are present", "missing": []}'
            ),
        ],
    )
    extra_edit = ToolResult(
        ok=True,
        summary="edited",
        metadata={"changed_files": ["quicksort.py"]},
    )
    extra_verify = ToolResult(
        ok=True,
        summary="verified",
        metadata={
            "verification": {
                "command": "pytest",
                "exit_code": 0,
                "duration_ms": 8,
                "output": "2 passed",
            }
        },
    )
    result = await AgentLoop(
        config=_config(tmp_path),
        model=model,
        tools=FakeTools([*_edit_then_verify(), extra_edit, extra_verify]),
        on_event=lambda kind, payload: events.append((kind, payload)),
    ).run("实现 heap_sort 和 quicksort，并补测试")

    judge_events = [payload for kind, payload in events if kind == "judge_finished"]
    assert result.status is AgentStatus.COMPLETED
    assert [payload.get("accepted") for payload in judge_events] == [False, True]
    assert any("[LLM Judge]" in (message.content or "") for message in model.calls[3])
    assert result.changed_files == ["heap_sort.py", "quicksort.py"]
    started = [payload for kind, payload in events if kind == "judge_started"]
    assert all(payload.get("type") == "prompt" for payload in started)


@pytest.mark.asyncio
async def test_judge_cap_allows_stop_after_two_blocks(tmp_path: Path) -> None:
    model = FakeModel(
        [
            ModelResponse(tool_calls=[ToolCall(id="1", name="fake")]),
            ModelResponse(tool_calls=[ToolCall(id="2", name="verify_changes")]),
            ModelResponse(text="Done"),
            ModelResponse(text="Still done"),
            ModelResponse(text="Stopping anyway"),
        ],
        judge_steps=[
            ModelResponse(text='{"accepted": false, "reason": "gap one", "missing": ["a"]}'),
            ModelResponse(text='{"accepted": false, "reason": "gap two", "missing": ["b"]}'),
        ],
    )
    result = await AgentLoop(
        config=_config(tmp_path),
        model=model,
        tools=FakeTools(_edit_then_verify()),
    ).run("实现 heap_sort 和测试")

    assert result.status is AgentStatus.COMPLETED
    assert len(model.judge_calls) == 2
    assert result.summary == "Stopping anyway"


@pytest.mark.asyncio
async def test_unparsable_prompt_hook_does_not_silently_pass(tmp_path: Path) -> None:
    events: list[str] = []
    model = FakeModel(
        [
            ModelResponse(tool_calls=[ToolCall(id="1", name="fake")]),
            ModelResponse(tool_calls=[ToolCall(id="2", name="verify_changes")]),
            ModelResponse(text="Done"),
            ModelResponse(text="Done after the inspector failed"),
        ],
        judge_steps=[
            ModelResponse(text="not json"),
            ModelResponse(text='{"accepted": true, "reason": "ok", "missing": []}'),
        ],
    )
    result = await AgentLoop(
        config=_config(tmp_path),
        model=model,
        tools=FakeTools(_edit_then_verify()),
        on_event=lambda kind, _payload: events.append(kind),
    ).run("实现 heap_sort 和测试")

    assert result.status is AgentStatus.COMPLETED
    assert events.count("hook_failed") == 1
    assert len(model.judge_calls) == 2


@pytest.mark.asyncio
async def test_hooks_json_can_disable_llm_judge(tmp_path: Path) -> None:
    forge = tmp_path / ".forge"
    forge.mkdir()
    (forge / "hooks.json").write_text('{"llm_judge": false}', encoding="utf-8")
    events: list[str] = []
    model = FakeModel(
        [
            ModelResponse(tool_calls=[ToolCall(id="1", name="fake")]),
            ModelResponse(tool_calls=[ToolCall(id="2", name="verify_changes")]),
            ModelResponse(text="Done"),
        ]
    )
    result = await AgentLoop(
        config=_config(tmp_path),
        model=model,
        tools=FakeTools(_edit_then_verify()),
        on_event=lambda kind, _payload: events.append(kind),
    ).run("实现 heap_sort 和测试")

    assert result.status is AgentStatus.COMPLETED
    assert "judge_started" not in events
    assert model.judge_calls == []


def test_command_hooks_are_not_enabled_by_default(tmp_path: Path) -> None:
    forge = tmp_path / ".forge"
    forge.mkdir()
    (forge / "hooks.json").write_text(
        json.dumps(
            {
                "llm_judge": False,
                "hooks": [
                    {
                        "id": "shell",
                        "event": HookEvent.STOP_ATTEMPTED.value,
                        "type": HookType.COMMAND.value,
                        "command": "echo hi",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config = load_hook_config(tmp_path)
    specs = stop_hooks(config)
    assert len(specs) == 1
    assert specs[0].type is HookType.COMMAND
    assert config.allow_command_hooks is False


def test_match_dangerous_command_catches_wrappers_not_tests() -> None:
    from forge_agent.hooks.dangerous import match_dangerous_command

    assert match_dangerous_command("rm -rf /") == "rm -rf"
    assert match_dangerous_command('sudo rm -rf dist') == "rm -rf"
    assert match_dangerous_command('bash -c "rm -rf /tmp/build"') == "rm -rf"
    assert match_dangerous_command("rd /s /q build") == "rd /s"
    assert match_dangerous_command("python -m pytest -q") is None
    assert match_dangerous_command("npm rm lodash") is None
    assert match_dangerous_command("git rm unused.py") is None


@pytest.mark.asyncio
async def test_before_tool_blocks_dangerous_bash_without_executing(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="1",
                        name="run_command",
                        arguments={"command": 'bash -c "rm -rf dist"'},
                    )
                ]
            ),
            ModelResponse(text="The hook blocked the destructive command."),
        ]
    )
    tools = FakeTools([ToolResult(ok=True, summary="should not run")])
    result = await AgentLoop(
        config=_config(tmp_path),
        model=model,
        tools=tools,
        on_event=lambda kind, payload: events.append((kind, payload)),
    ).run("clean the build")

    denied = [payload for kind, payload in events if kind == "hook_denied"]
    finished = [payload for kind, payload in events if kind == "tool_finished"]
    assert result.status is AgentStatus.COMPLETED
    assert tools.calls == []
    assert denied and denied[0].get("pattern") == "rm -rf"
    assert denied[0].get("event") == "before_tool"
    assert finished and finished[0].get("error_code") == "hook_denied"


@pytest.mark.asyncio
async def test_dangerous_bash_hook_can_be_disabled(tmp_path: Path) -> None:
    forge = tmp_path / ".forge"
    forge.mkdir()
    (forge / "hooks.json").write_text(
        '{"llm_judge": false, "block_dangerous_bash": false}',
        encoding="utf-8",
    )
    tools = FakeTools([ToolResult(ok=True, summary="ran")])
    result = await AgentLoop(
        config=_config(tmp_path),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="1",
                            name="run_command",
                            arguments={"command": "rm -rf dist"},
                        )
                    ]
                ),
                ModelResponse(text="Ran after the hook was disabled."),
            ]
        ),
        tools=tools,
    ).run("clean")

    assert result.status is AgentStatus.COMPLETED
    assert [call.name for call in tools.calls] == ["run_command"]


def test_before_tool_hooks_include_secret_shell_by_default() -> None:
    ids = [spec.id for spec in before_tool_hooks(HookConfig())]
    assert ids[:2] == ["block_dangerous_bash", "block_secret_shell"]


def test_match_secret_or_escape_command_catches_secrets_not_tests(tmp_path: Path) -> None:
    workspace = tmp_path.resolve()
    (workspace / "readme.txt").write_text("ok\n", encoding="utf-8")
    assert match_secret_or_escape_command("type .env", workspace) == ".env"
    assert match_secret_or_escape_command("Get-Content .env", workspace) == ".env"
    assert match_secret_or_escape_command("cat ~/.ssh/id_rsa", workspace) == "credential directory"
    assert match_secret_or_escape_command(
        r"copy readme.txt C:\Users\someone\Desktop\stolen.txt",
        workspace,
    ) in {"path outside workspace", "copy outside workspace"}
    assert (
        match_secret_or_escape_command(
            "python -c \"print(open('.env').read())\"",
            workspace,
        )
        == ".env"
    )
    assert match_secret_or_escape_command("type .env.example", workspace) is None
    assert match_secret_or_escape_command("python -m pytest -q", workspace) is None
    assert match_secret_or_escape_command("$env:PATH", workspace) is None
    python_in_home = (
        r"C:\Users\lenovo\AppData\Local\Programs\Python\Python312\python.exe -m pytest"
    )
    assert match_secret_or_escape_command(python_in_home, workspace) is None
    inside = workspace / "script.py"
    assert match_secret_or_escape_command(f"python {inside}", workspace) is None


@pytest.mark.asyncio
async def test_before_tool_blocks_secret_shell_without_executing(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    tools = FakeTools([ToolResult(ok=True, summary="should not run")])
    result = await AgentLoop(
        config=_config(tmp_path),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="1",
                            name="run_command",
                            arguments={"command": "type .env"},
                        )
                    ]
                ),
                ModelResponse(text="The hook blocked reading secrets."),
            ]
        ),
        tools=tools,
        on_event=lambda kind, payload: events.append((kind, payload)),
    ).run("print the secrets")

    denied = [payload for kind, payload in events if kind == "hook_denied"]
    finished = [payload for kind, payload in events if kind == "tool_finished"]
    assert result.status is AgentStatus.COMPLETED
    assert tools.calls == []
    assert denied and denied[0].get("hook_id") == "block_secret_shell"
    assert denied[0].get("pattern") == ".env"
    assert denied[0].get("event") == "before_tool"
    assert finished and finished[0].get("error_code") == "hook_denied"


@pytest.mark.asyncio
async def test_before_tool_blocks_copy_outside_workspace(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    tools = FakeTools([ToolResult(ok=True, summary="should not run")])
    result = await AgentLoop(
        config=_config(tmp_path),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="1",
                            name="run_command",
                            arguments={
                                "command": r"copy readme.txt C:\Users\someone\Desktop\out.txt",
                            },
                        )
                    ]
                ),
                ModelResponse(text="The hook blocked copying out of the workspace."),
            ]
        ),
        tools=tools,
        on_event=lambda kind, payload: events.append((kind, payload)),
    ).run("copy the file away")

    denied = [payload for kind, payload in events if kind == "hook_denied"]
    assert result.status is AgentStatus.COMPLETED
    assert tools.calls == []
    assert denied and denied[0].get("hook_id") == "block_secret_shell"


@pytest.mark.asyncio
async def test_secret_shell_hook_can_be_disabled(tmp_path: Path) -> None:
    forge = tmp_path / ".forge"
    forge.mkdir()
    (forge / "hooks.json").write_text(
        '{"llm_judge": false, "block_secret_shell": false}',
        encoding="utf-8",
    )
    tools = FakeTools([ToolResult(ok=True, summary="ran")])
    result = await AgentLoop(
        config=_config(tmp_path),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="1",
                            name="run_command",
                            arguments={"command": "type .env"},
                        )
                    ]
                ),
                ModelResponse(text="Ran after the hook was disabled."),
            ]
        ),
        tools=tools,
    ).run("print env")

    assert result.status is AgentStatus.COMPLETED
    assert [call.name for call in tools.calls] == ["run_command"]
