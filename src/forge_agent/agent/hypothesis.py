"""Structured hypothesis tracking for failed verification."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DebugHypothesis(BaseModel):
    signature: str
    observed_failure: str
    hypothesis: str
    required_evidence: list[str] = Field(default_factory=list)
    experiments: int = 0
    last_result: str | None = None
    retired: bool = False

    @classmethod
    def from_verification(
        cls,
        *,
        command: str,
        exit_code: int,
        workspace_version: int,
        output: str,
    ) -> DebugHypothesis:
        excerpt = output.strip()[-400:] or "(no output)"
        return cls(
            signature=f"{workspace_version}|{command}|{exit_code}",
            observed_failure=f"`{command}` exited {exit_code}",
            hypothesis=(
                "The latest workspace version still fails this verification command; "
                "inspect the command output and make a minimal correction."
            ),
            required_evidence=[
                "Updated file diff after the failure",
                f"A later `{command}` run on the newest workspace version",
            ],
            experiments=1,
            last_result=excerpt,
        )
