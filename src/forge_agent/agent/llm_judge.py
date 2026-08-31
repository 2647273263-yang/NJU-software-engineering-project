"""Independent LLM acceptance inspector. Does not replace CompletionJudge."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from forge_agent.agent.state import AgentState
from forge_agent.types import Message, VerificationRecord

JUDGE_SYSTEM = """You are an independent acceptance inspector (LLM Judge) for a local coding agent.
The main agent wants to stop. Decide whether the ORIGINAL user task is actually complete.
You are not the author. Do not praise style. Do not ask for refactors the user did not request.
Use only the supplied evidence. A passing test is not enough if files or cases are missing.
Return strict JSON only:
{"accepted": true or false, "reason": "one short sentence", "missing": ["remaining item"]}
If accepted is true, missing must be [].
"""


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    accepted: bool
    reason: str
    missing: list[str] = field(default_factory=list)
    parse_error: bool = False

    def inject_message(self) -> str:
        gaps = self.missing or (
            [self.reason] if self.reason else ["The original task is still incomplete."]
        )
        lines = [
            "[LLM Judge] The acceptance inspector blocked stopping.",
            self.reason or "The original user task is not done.",
            "Still missing:",
            *(f"- {item}" for item in gaps[:8]),
            "Continue the original task. Do not claim completion until these items are done.",
        ]
        return "\n".join(lines)


_COMPLEX_MARKERS = (
    "\n- ",
    "\n* ",
    "\n1.",
    "\n1)",
    "并且",
    "同时",
    "以及",
    "实现",
    "添加",
    "重构",
    "多个",
    "全部",
    "每个",
    " and ",
    " then ",
    "also",
    "implement",
    "add ",
    "fix ",
)


def is_complex_coding_stop(task: str, state: AgentState) -> bool:
    """Judge only when this run actually edited code and the task is more than a one-liner Q&A."""

    if not state.run_changed_files:
        return False
    if len(state.run_changed_files) >= 2:
        return True
    if state.steps >= 3:
        return True
    lowered = f" {task.lower()} "
    if any(marker in task or marker in lowered for marker in _COMPLEX_MARKERS):
        return True
    return len(task.strip()) >= 80


def build_judge_user_payload(
    *,
    task: str,
    last_reply: str,
    changed_files: list[str],
    verification: VerificationRecord | None,
    evidence_lines: list[str],
) -> str:
    parts = [
        f"Original user task:\n{task.strip()[:2000] or '(empty)'}",
        "Changed files this run:\n"
        + ("\n".join(f"- {path}" for path in changed_files) or "(none)"),
    ]
    if verification is None:
        parts.append("Verification: none")
    else:
        parts.append(
            "Verification:\n"
            f"- command: {verification.command}\n"
            f"- exit_code: {verification.exit_code}\n"
            f"- passed: {verification.passed}"
        )
    if evidence_lines:
        parts.append("Evidence ledger:\n" + "\n".join(f"- {line}" for line in evidence_lines[:16]))
    parts.append("Main agent last reply:\n" + (last_reply.strip()[:1500] or "(empty)"))
    parts.append("Return JSON only.")
    return "\n\n".join(parts)


def judge_messages(*, system_prompt: str, payload: str) -> list[Message]:
    return [
        Message(role="system", content=system_prompt.strip() or JUDGE_SYSTEM),
        Message(role="user", content=payload),
    ]


def parse_judge_response(text: str) -> JudgeVerdict:
    data = _parse_object(text)
    if data is None:
        return JudgeVerdict(
            accepted=False,
            reason="The inspector returned output that could not be parsed as JSON.",
            missing=["Re-check the original task against the files actually changed."],
            parse_error=True,
        )
    accepted = data.get("accepted")
    if not isinstance(accepted, bool):
        return JudgeVerdict(
            accepted=False,
            reason="The inspector JSON omitted a boolean accepted field.",
            missing=["Re-check the original task against the files actually changed."],
            parse_error=True,
        )
    reason = str(data.get("reason") or "").strip() or (
        "The inspector accepted the run." if accepted else "The inspector rejected the run."
    )
    missing_raw = data.get("missing") or []
    missing = (
        [str(item).strip() for item in missing_raw if str(item).strip()]
        if isinstance(missing_raw, list)
        else []
    )
    if accepted:
        return JudgeVerdict(accepted=True, reason=reason, missing=[])
    if not missing:
        missing = [reason]
    return JudgeVerdict(accepted=False, reason=reason, missing=missing[:8])


def _parse_object(text: str) -> dict[str, object] | None:
    stripped = _strip_json_fence(text)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if match is None:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped
