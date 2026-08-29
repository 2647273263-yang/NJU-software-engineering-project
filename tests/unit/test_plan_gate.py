from forge_agent.agent.plan_gate import uses_readonly_plan, wants_plan_first
from forge_agent.types import RunMode


def test_plan_mode_is_readonly_but_does_not_gate_execution() -> None:
    assert uses_readonly_plan("分析工作区", mode=RunMode.PLAN)
    assert not wants_plan_first("分析工作区", mode=RunMode.PLAN)
    assert not wants_plan_first("Fix the failing test.", mode=RunMode.PLAN)


def test_agent_mode_detects_plan_first_phrases() -> None:
    assert wants_plan_first("先给我方案，再改代码", mode=RunMode.BUILD)
    assert wants_plan_first("先给出方案，再执行", mode=RunMode.BUILD)
    assert wants_plan_first("先分析方案，再进行更改", mode=RunMode.BUILD)
    assert wants_plan_first("先分析文件，再进行修改", mode=RunMode.BUILD)
    assert wants_plan_first("先出一份实现方案", mode=RunMode.BUILD)
    assert wants_plan_first("分析可行性与实现方案", mode=RunMode.BUILD)
    assert wants_plan_first("Give me a plan first, then implement", mode=RunMode.BUILD)
    assert wants_plan_first("don't implement yet", mode=RunMode.BUILD)
    assert uses_readonly_plan("先给我方案", mode=RunMode.BUILD)


def test_agent_mode_does_not_gate_ordinary_tasks() -> None:
    assert not wants_plan_first("Fix add() and verify it.", mode=RunMode.BUILD)
    assert not wants_plan_first("按这个方案直接改 app.py", mode=RunMode.BUILD)
    assert not wants_plan_first("分析每一个排序的优点", mode=RunMode.BUILD)
    assert not wants_plan_first("这是分析任务，无需修改代码", mode=RunMode.BUILD)
    assert not wants_plan_first("", mode=RunMode.BUILD)
    assert not uses_readonly_plan("Fix add() and verify it.", mode=RunMode.BUILD)
    assert not uses_readonly_plan("分析每一个排序的优点", mode=RunMode.BUILD)
