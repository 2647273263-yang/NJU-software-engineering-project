"""Best-effort redaction for logs and exported trajectories."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SENSITIVE_KEY = re.compile(
    r"(?:^|[-_])(?:api[-_]?key|access[-_]?token|auth[-_]?token|client[-_]?secret|"
    r"token|secret|password|passwd|authorization)(?:$|[-_])",
    re.I,
)
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I)
LIKELY_API_KEY = re.compile(
    r"\b(?:(?:sk|key)-(?:proj-)?[A-Za-z0-9_-]{12,}|gh[opusr]_[A-Za-z0-9]{20,}|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16})\b"
)
PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----.*?"
    r"-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----",
    re.DOTALL,
)
WINDOWS_USER_PATH = re.compile(
    r"(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/\s:\"<>|?*]+(?:[\\/][^\s\"<>|?*]*)?"
)
SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<prefix>
        \b(?:api[-_]?key|access[-_]?token|auth[-_]?token|client[-_]?secret|
        password|passwd|secret)\b
        \s*(?::|=)\s*
    )
    (?P<quote>["']?)
    [^\s"'`,;]+
    (?P=quote)
    """
)


def redact_text(text: str, *, workspace: Path | None = None) -> str:
    redacted = EMAIL.sub("[REDACTED_EMAIL]", text)
    redacted = BEARER.sub("Bearer [REDACTED]", redacted)
    redacted = LIKELY_API_KEY.sub("[REDACTED_KEY]", redacted)
    redacted = PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", redacted)
    redacted = SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]", redacted
    )
    if workspace is not None:
        redacted = _replace_path(redacted, workspace, "$WORKSPACE")
    redacted = WINDOWS_USER_PATH.sub(  # forge-release: allow
        lambda _: r"C:\Users\[REDACTED_USER]", redacted  # forge-release: allow
    )
    home = Path.home()
    redacted = _replace_path(redacted, home, "$HOME")
    username = os.environ.get("USERNAME") or os.environ.get("USER")
    if username and len(username) >= 3:
        redacted = re.sub(re.escape(username), "[REDACTED_USER]", redacted, flags=re.I)
    return redacted


def redact_data(value: Any, *, workspace: Path | None = None) -> Any:
    if isinstance(value, str):
        return redact_text(value, workspace=workspace)
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if SENSITIVE_KEY.search(str(key))
                else redact_data(item, workspace=workspace)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_data(item, workspace=workspace) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item, workspace=workspace) for item in value)
    return value


def _replace_path(text: str, path: Path, replacement: str) -> str:
    variants = {str(path), path.as_posix()}
    result = text
    for variant in sorted(variants, key=len, reverse=True):
        if variant:
            result = re.sub(re.escape(variant), replacement, result, flags=re.I)
    return result
