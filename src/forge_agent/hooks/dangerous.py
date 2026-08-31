"""Deterministic matcher for destructive shell commands."""

from __future__ import annotations

import re

_COMMAND_TOOLS = frozenset({"run_command", "verify_changes"})

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "rm -rf",
        re.compile(
            r"(?i)\brm(?:\.exe)?\b[^|&;\n]{0,80}"
            r"(?:-[A-Za-z]*r[A-Za-z]*|--recursive)\b"
        ),
    ),
    (
        "rd /s",
        re.compile(r"(?i)\b(?:rd|rmdir)(?:\.exe)?\b[^|&\n]{0,60}/s\b"),
    ),
    (
        "del /s",
        re.compile(r"(?i)\bdel(?:\.exe)?\b[^|&\n]{0,60}/s\b"),
    ),
    (
        "Remove-Item -Recurse",
        re.compile(r"(?i)\bremove-item\b[^|&\n]{0,80}-(?:r|recurse)\b"),
    ),
    (
        "format drive",
        re.compile(r"(?i)\bformat(?:\.com)?\s+[a-z]:"),
    ),
    (
        "shutdown",
        re.compile(r"(?i)(?:^|[\s;&|])(?:shutdown|reboot|diskpart|mkfs)\b"),
    ),
    (
        "dd of=/dev",
        re.compile(r"(?i)\bdd\b[^|&\n]{0,120}\bof=/dev/"),
    ),
    (
        "curl | sh",
        re.compile(r"(?i)\b(?:curl|wget)\b[^|&\n]{0,200}\|\s*(?:sh|bash|zsh|pwsh|powershell)\b"),
    ),
    (
        "git reset --hard",
        re.compile(r"(?i)\bgit\s+reset\s+--hard\b"),
    ),
    (
        "git clean -f",
        re.compile(r"(?i)\bgit\s+clean\s+-[A-Za-z]*f"),
    ),
    (
        "git push --force",
        re.compile(r"(?i)\bgit\s+push\b[^|&\n]{0,80}(?:--force\b|-[A-Za-z]*f\b)"),
    ),
    (
        "fork bomb",
        re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;"),
    ),
    (
        "shutil.rmtree",
        re.compile(r"(?i)\bshutil\.rmtree\s*\("),
    ),
    (
        "os.remove",
        re.compile(r"(?i)\bos\.(?:remove|unlink)\s*\("),
    ),
)


def match_dangerous_command(command: str) -> str | None:
    """Return a short pattern name if the shell text is a destructive wipe."""

    text = command.strip()
    if not text:
        return None
    for name, pattern in _PATTERNS:
        if pattern.search(text):
            return name
    return None


def dangerous_command_reason(pattern: str) -> str:
    return (
        f"Hook block_dangerous_bash blocked this command ({pattern}). "
        "Destructive shell commands are never executed, even if approved."
    )


def tool_carries_shell(tool_name: str) -> bool:
    return tool_name in _COMMAND_TOOLS
