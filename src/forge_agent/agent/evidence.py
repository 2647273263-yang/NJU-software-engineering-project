"""Evidence-backed completion summary."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from forge_agent.types import RunResult


class ClaimStatus(StrEnum):
    PROVEN = "proven"
    UNPROVEN = "unproven"
    UNVERIFIABLE = "unverifiable"


class Evidence(BaseModel):
    kind: str
    description: str
    reference: str | None = None


class Claim(BaseModel):
    statement: str
    status: ClaimStatus
    evidence: list[Evidence] = Field(default_factory=list)


class EvidenceLedger(BaseModel):
    claims: list[Claim] = Field(default_factory=list)

    @classmethod
    def from_run_result(
        cls,
        result: RunResult,
        *,
        events: Iterable[tuple[str, dict[str, Any]]] = (),
    ) -> EvidenceLedger:
        event_list = list(events)
        claims: list[Claim] = []
        if result.changed_files:
            diff_by_path = cls._diff_evidence(event_list)
            claims.append(
                Claim(
                    statement=f"Changed {len(result.changed_files)} file(s)",
                    status=ClaimStatus.PROVEN,
                    evidence=[
                        Evidence(
                            kind="file_change",
                            description=diff_by_path.get(path, path),
                            reference=path,
                        )
                        for path in result.changed_files
                    ],
                )
            )
        if result.verification is not None:
            verification = result.verification
            claims.append(
                Claim(
                    statement=f"Verification command `{verification.command}` passed",
                    status=(
                        ClaimStatus.PROVEN if verification.passed else ClaimStatus.UNPROVEN
                    ),
                    evidence=[
                        Evidence(
                            kind="command",
                            description=(
                                f"exit={verification.exit_code}, "
                                f"duration={verification.duration_ms}ms"
                            ),
                            reference=verification.command,
                        )
                    ],
                )
            )
        elif result.changed_files:
            claims.append(
                Claim(
                    statement="The latest workspace changes were verified",
                    status=ClaimStatus.UNPROVEN,
                    evidence=[
                        Evidence(
                            kind="missing_verification",
                            description="No verification result exists for the latest edit",
                        )
                    ],
                )
            )
        verification_events = [
            payload
            for kind, payload in event_list
            if kind == "tool_finished"
            and payload.get("name") == "verify_changes"
            and isinstance(payload.get("metadata"), dict)
        ]
        failed = [payload for payload in verification_events if not payload.get("ok")]
        passed = [payload for payload in verification_events if payload.get("ok")]
        if failed and passed:
            claims.append(
                Claim(
                    statement="Recovered from a failed verification",
                    status=ClaimStatus.PROVEN,
                    evidence=[
                        Evidence(
                            kind="failure",
                            description=str(failed[-1].get("summary", "verification failed")),
                        ),
                        Evidence(
                            kind="recovery",
                            description=str(passed[-1].get("summary", "verification passed")),
                        ),
                    ],
                )
            )
        hypotheses = [
            payload
            for kind, payload in event_list
            if kind == "hypothesis_updated"
        ]
        retired = [payload for payload in hypotheses if payload.get("retired")]
        if retired:
            last = retired[-1]
            claims.append(
                Claim(
                    statement=str(last.get("hypothesis", "Debug hypothesis retired")),
                    status=ClaimStatus.UNVERIFIABLE,
                    evidence=[
                        Evidence(
                            kind="hypothesis",
                            description=str(last.get("observed_failure", "")),
                            reference=str(last.get("signature")),
                        )
                    ],
                )
            )
        elif hypotheses and result.verification is not None and not result.verification.passed:
            last = hypotheses[-1]
            claims.append(
                Claim(
                    statement=str(last.get("hypothesis", "Active debug hypothesis")),
                    status=ClaimStatus.UNPROVEN,
                    evidence=[
                        Evidence(
                            kind="hypothesis",
                            description=str(last.get("observed_failure", "")),
                        )
                    ],
                )
            )
        summary = result.workspace_summary
        leftover = int(summary.get("untracked", 0) or 0) + len(
            summary.get("changed_entries") or []
        )
        if summary.get("available") and leftover > 0:
            claims.append(
                Claim(
                    statement="Working tree snapshot after the run",
                    status=ClaimStatus.PROVEN,
                    evidence=[
                        Evidence(
                            kind="git_status",
                            description=(
                                f"{len(summary.get('changed_entries', []) or [])} changed, "
                                f"{summary.get('untracked', 0)} untracked, "
                                f"+{summary.get('insertions', 0)}/"
                                f"-{summary.get('deletions', 0)}"
                            ),
                        )
                    ],
                )
            )
        judge_events = [payload for kind, payload in event_list if kind == "judge_finished"]
        if judge_events:
            last = judge_events[-1]
            accepted = bool(last.get("accepted"))
            reason = str(last.get("reason") or "").strip()
            missing = last.get("missing")
            missing_items = (
                [str(item) for item in missing if str(item).strip()]
                if isinstance(missing, list)
                else []
            )
            claims.append(
                Claim(
                    statement=(
                        "LLM Judge accepted the run"
                        if accepted
                        else "LLM Judge blocked stopping"
                    ),
                    status=ClaimStatus.PROVEN if accepted else ClaimStatus.UNPROVEN,
                    evidence=[
                        Evidence(
                            kind="llm_judge",
                            description=reason or ("accepted" if accepted else "blocked"),
                            reference="; ".join(missing_items[:6]) or None,
                        )
                    ],
                )
            )
        return cls(claims=claims)

    @staticmethod
    def _diff_evidence(
        events: list[tuple[str, dict[str, Any]]],
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for kind, payload in events:
            if kind != "tool_finished":
                continue
            metadata = payload.get("metadata")
            if not isinstance(metadata, dict):
                continue
            changed = metadata.get("changed_files")
            if not isinstance(changed, list):
                continue
            content = payload.get("content")
            for path in changed:
                if isinstance(path, str):
                    result[path] = str(content or path)
        return result
