"""Block shell that reads secrets or copies files out of the workspace."""

from __future__ import annotations

import os
import re
from pathlib import Path

from forge_agent.tools.sensitive import sensitive_read_reason

_COPY_COMMANDS = re.compile(
    r"(?i)(?:^|[\s;&|])(?:copy|xcopy|robocopy|move|cp|mv|copy-item|move-item|cpi|mi)\b"
)
_HOME_ENV = re.compile(
    r"(?i)^(?:~|%USERPROFILE%|%HOMEDRIVE%%HOMEPATH%|%HOME%|"
    r"\$HOME|\$env:USERPROFILE|\$env:HOME)$"
)
_HOME_PREFIX = re.compile(
    r"(?i)^(?:~[/\\]|%USERPROFILE%|%HOMEDRIVE%%HOMEPATH%|%HOME%|"
    r"\$HOME(?:[/\\]|$)|\$env:USERPROFILE|\$env:HOME)"
)
_USER_HOME_ABS = re.compile(r"(?i)^(?:/home/|/Users/|/root/|[A-Za-z]:\\Users\\)")
_ENV_ONLY = re.compile(
    r"(?i)^(?:%[A-Za-z][A-Za-z0-9_]*%|\$env:[A-Za-z][A-Za-z0-9_]*|\$[A-Za-z_][A-Za-z0-9_]*)$"
)
_QUOTED = re.compile(r"\"([^\"]+)\"|'([^']+)'|`([^`]+)`")
_BARE_TOKEN = re.compile(r"[^\s|&;<>()]+")
_REDIRECT_TARGET = re.compile(
    r"(?i)(?:>>?|2>>?)\s*(\"[^\"]+\"|'[^']+'|[^\s|&;]+)"
    r"|(?:out-file|set-content|add-content|tee-object)\s+(?:-filepath\s+)?"
    r"(\"[^\"]+\"|'[^']+'|[^\s|&;]+)"
)
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_TOOL_SUFFIXES = frozenset({".exe", ".cmd", ".bat", ".ps1", ".com"})


def match_secret_or_escape_command(
    command: str,
    workspace: Path | None = None,
) -> str | None:
    """Return a short pattern name if the shell reads secrets or leaves the workspace."""

    text = command.strip()
    if not text:
        return None
    root = workspace.expanduser().resolve() if workspace is not None else None
    tokens = _path_tokens(text)
    for token in tokens:
        secret = _secret_reason(token)  # forge-release: allow
        if secret is not None:
            return secret
        if _is_escape(token, root):
            return "path outside workspace"
    if _COPY_COMMANDS.search(text):
        for token in reversed(tokens):
            if _looks_like_path(token) and _is_escape(token, root):
                return "copy outside workspace"
    return None


def secret_shell_reason(pattern: str) -> str:
    return (
        f"Hook block_secret_shell blocked this command ({pattern}). "
        "Shell cannot read secret files or copy out of the workspace, even if approved."
    )


def _path_tokens(command: str, *, depth: int = 0) -> list[str]:
    found: list[str] = []
    if depth > 3:
        return found

    def add(token: str) -> None:
        cleaned = _unquote(token)
        if not cleaned or cleaned in found:
            return
        found.append(cleaned)
        for variant in _token_variants(cleaned):
            if variant not in found:
                found.append(variant)
        if depth < 3 and any(mark in cleaned for mark in "\"'`"):
            for inner in _path_tokens(cleaned, depth=depth + 1):
                if inner not in found:
                    found.append(inner)

    for match in _QUOTED.finditer(command):
        add(next(group for group in match.groups() if group))
    for match in _REDIRECT_TARGET.finditer(command):
        add(next(group for group in match.groups() if group))
    for match in _BARE_TOKEN.finditer(command):
        add(match.group(0).strip("&"))
    return found


def _token_variants(token: str) -> list[str]:
    variants: list[str] = []
    if "=" in token and not _DRIVE.match(token):
        variants.append(token.rsplit("=", 1)[-1])
    return [item for item in variants if item and item != token]


def _unquote(token: str) -> str:
    text = token.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"\"", "'", "`"}:
        return text[1:-1]
    return text


def _secret_reason(token: str) -> str | None:
    posix = token.replace("\\", "/")
    while posix.startswith("./"):
        posix = posix[2:]
    parts = [part for part in posix.split("/") if part and part != "."]
    candidates = [posix]
    if "/" in posix:
        candidates.append(posix.rsplit("/", 1)[-1])
    candidates.extend("/".join(parts[index:]) for index in range(len(parts)))
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        reason = sensitive_read_reason(candidate)
        if reason is not None:
            return reason
    return None


def _is_escape(token: str, workspace: Path | None) -> bool:
    raw = token.strip()
    if not raw or re.match(r"(?i)^https?://", raw):
        return False
    path = Path(raw.replace("/", os.sep))
    if ".." in path.parts:
        return True
    if _is_tool_binary(path):
        return False
    expanded = os.path.expandvars(os.path.expanduser(raw))
    candidate = Path(expanded)
    if _is_tool_binary(candidate):
        return False
    if _inside_workspace(candidate, workspace):
        return False
    if _is_home_ref(raw):
        return True
    if _ENV_ONLY.fullmatch(raw):
        return False
    if workspace is None:
        return False
    return candidate.is_absolute()


def _inside_workspace(candidate: Path, workspace: Path | None) -> bool:
    if workspace is None or not candidate.is_absolute():
        return False
    try:
        candidate.resolve(strict=False).relative_to(workspace)
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _is_home_ref(raw: str) -> bool:
    return bool(_HOME_ENV.fullmatch(raw) or _HOME_PREFIX.search(raw) or _USER_HOME_ABS.search(raw))


def _looks_like_path(token: str) -> bool:
    return bool(re.search(r"[\\/.:~%]", token)) or token in {".", ".."}


def _is_tool_binary(path: Path) -> bool:
    return path.suffix.lower() in _TOOL_SUFFIXES
