"""Conservative, explainable risk classification for tool calls."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from forge_agent.types import RunMode

READ_ONLY_TOOLS = frozenset(
    {"read_file", "list_files", "search_text", "git_diff", "git_status", "repo_outline"}
)
PARALLEL_READ_LIMIT = 4


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PolicyDecision(BaseModel):
    allowed: bool
    risk: RiskLevel
    requires_approval: bool
    reason: str


class PolicyEngine:
    _READ_ONLY = READ_ONLY_TOOLS
    _MUTATING = frozenset(
        {
            "replace_in_file",
            "write_file",
            "undo_last_edit",
            "rollback_changes",
            "verify_changes",
        }
    )
    _INSTALL_COMMAND = re.compile(
        r"(?i)\b(?:pip|pip3|uv|poetry|npm|pnpm|yarn|bun|cargo|go)\b.*\b(?:install|add|i)\b"
    )
    _NETWORK_COMMAND = re.compile(r"(?i)\b(?:curl|wget|invoke-webrequest)\b")
    _DANGEROUS_COMMAND = re.compile(
        r"(?i)(?:^|[;&|]\s*)(?:rm|rmdir|del|format|shutdown|reboot)\b"
        r"|git\s+(?:reset\s+--hard|clean\s+-[a-z]*f|push\b)"
        r"|(?:curl|wget)\b.*\|\s*(?:sh|bash|pwsh|powershell)\b"
    )

    def __init__(
        self,
        *,
        mode: RunMode = RunMode.BUILD,
        auto_approve: bool = False,
        planning_pass: bool = False,
    ) -> None:
        self.mode = mode
        self.auto_approve = auto_approve
        self.planning_pass = planning_pass

    def evaluate(self, tool_name: str, arguments: dict[str, Any]) -> PolicyDecision:
        risk, reason = self.classify(tool_name, arguments)
        mutating = tool_name in self._MUTATING or tool_name == "run_command"
        planning = self.mode == RunMode.PLAN or self.planning_pass
        allowed = risk != RiskLevel.HIGH and not (planning and mutating)
        requires_approval = (
            allowed and risk == RiskLevel.MEDIUM and not self.auto_approve
        )
        if planning and mutating:
            reason = (
                "planning pass: wait for the user to confirm the plan before editing"
                if self.planning_pass
                else "mutating tools are disabled in plan mode"
            )
        return PolicyDecision(
            allowed=allowed,
            risk=risk,
            requires_approval=requires_approval,
            reason=reason,
        )

    def classify(self, tool_name: str, arguments: dict[str, Any]) -> tuple[RiskLevel, str]:
        if tool_name in self._READ_ONLY:
            return RiskLevel.LOW, "read-only workspace operation"
        if tool_name in self._MUTATING:
            path = str(arguments.get("path", ""))
            if path == ".git" or path.startswith((".git/", ".git\\")):
                return RiskLevel.HIGH, "direct Git metadata modification"
            return RiskLevel.MEDIUM, "workspace content modification"
        if tool_name in {"run_command", "verify_changes"}:
            command = str(arguments.get("command", ""))
            if self._DANGEROUS_COMMAND.search(command):
                return RiskLevel.HIGH, "command matches a destructive pattern"
            if self._INSTALL_COMMAND.search(command):
                return RiskLevel.MEDIUM, "installing dependencies can change the workspace"
            if self._NETWORK_COMMAND.search(command):
                return RiskLevel.MEDIUM, "network commands can cause side effects"
            if tool_name == "verify_changes":
                return RiskLevel.MEDIUM, "verification commands can cause side effects"
            return RiskLevel.MEDIUM, "commands can cause side effects"
        return RiskLevel.HIGH, "unknown tool is treated conservatively"
