"""Tool-free memory extractor that runs after a successful agent loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from forge_agent.context.compaction import CompactionSummary
from forge_agent.context.memory import (
    MemoryItem,
    append_memories,
    memory_auto_extract,
    sanitize_candidate,
)
from forge_agent.model.base import ModelClient
from forge_agent.types import Message

EXTRACT_SYSTEM = """You extract durable memories for a local coding agent.
Return a JSON array. Each object has keys: kind, text, tags, evidence.
kind must be one of: preference, convention, pitfall.
text is one short sentence, max 200 characters, in the user's language.
tags are short tokens such as tool names or languages.
Only record things that would still matter in a NEW chat next week:
- preference: how the user wants you to work (language, libraries, reply style)
- convention: how they want code written and verified (pytest vs unittest, formatting)
- pitfall: mistakes that already bit this user, with enough detail to avoid repeating them
Do NOT record project inventory: file lists, default algorithms, module maps, or what the repo contains.
Those belong in the codebase, not memory.
Skip secrets, API keys, .env values, passwords, and absolute machine paths.
Skip one-off chatter such as "I just ran the tests this turn".
If nothing in those three categories happened, return [].
"""


async def extract_run_memories(
    *,
    workspace: Path,
    model: ModelClient,
    task: str,
    messages: list[Message],
    result_summary: str,
    summary: CompactionSummary | None,
    evidence_lines: list[str],
    session_id: str,
    timeout_s: float = 45.0,
) -> list[MemoryItem]:
    if not memory_auto_extract(workspace):
        return []
    payload = _extract_payload(
        task=task,
        messages=messages,
        result_summary=result_summary,
        summary=summary,
        evidence_lines=evidence_lines,
    )
    response = await model.complete(
        [
            Message(role="system", content=EXTRACT_SYSTEM),
            Message(role="user", content=payload),
        ],
        [],
        timeout_s=timeout_s,
    )
    parsed = _parse_items(response.text or "")
    candidates = [
        item
        for raw in parsed
        if isinstance(raw, dict)
        for item in [sanitize_candidate(raw, workspace=workspace, session_id=session_id)]
        if item is not None
    ]
    return append_memories(workspace, candidates)


def _extract_payload(
    *,
    task: str,
    messages: list[Message],
    result_summary: str,
    summary: CompactionSummary | None,
    evidence_lines: list[str],
) -> str:
    users = [
        (message.content or "").strip()[:500]
        for message in messages
        if message.role == "user" and (message.content or "").strip()
    ][-6:]
    parts = [
        f"Latest task:\n{task[:800]}",
        "Recent user messages:\n" + ("\n".join(f"- {item}" for item in users) or "(none)"),
    ]
    if summary is not None:
        parts.append("Compaction summary:\n" + summary.render()[:1500])
    if evidence_lines:
        parts.append("Evidence:\n" + "\n".join(f"- {line}" for line in evidence_lines[:12]))
    if result_summary.strip():
        parts.append("Run summary:\n" + result_summary.strip()[:800])
    return "\n\n".join(parts)


def _parse_items(text: str) -> list[Any]:
    stripped = _strip_json_fence(text)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("[")
        end = stripped.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            data = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return []
    return data if isinstance(data, list) else []


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped
