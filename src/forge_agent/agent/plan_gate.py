"""Deterministic detection of “plan first” user intent."""

from __future__ import annotations

import re

from forge_agent.types import RunMode

_PLAN_FIRST = (
    re.compile(r"先.{0,12}(方案|计划|规划|设计)"),
    re.compile(r"先.{0,16}(分析|查看|检查)"),
    re.compile(r"先.{0,20}再.{0,12}(改|修改|更改|执行|实现|动手)"),
    re.compile(r"(给我|给个|出一份|出个|给一份).{0,8}(方案|计划)"),
    re.compile(r"(可行性).{0,12}(实现)?方案"),
    re.compile(r"不要?(先)?(改代码|动手|实现|编码)"),
    re.compile(r"\bplan first\b", re.IGNORECASE),
    re.compile(r"give me (a |the )?plan\b", re.IGNORECASE),
    re.compile(r"don'?t (code|implement|edit) yet", re.IGNORECASE),
    re.compile(r"do not (code|implement) yet", re.IGNORECASE),
)


def wants_plan_first(text: str, *, mode: RunMode) -> bool:
    """True when Agent/Build text asks to plan before any edits.

    Plan mode is not gated this way: it only delivers analysis/plan and stops.
    """

    if mode is RunMode.PLAN:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    return any(pattern.search(stripped) for pattern in _PLAN_FIRST)


def uses_readonly_plan(text: str, *, mode: RunMode) -> bool:
    """True when this turn must stay read-only (Plan mode or plan-first Agent text)."""

    return mode is RunMode.PLAN or wants_plan_first(text, mode=mode)
