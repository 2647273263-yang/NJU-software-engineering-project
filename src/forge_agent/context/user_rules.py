"""Optional user-editable rules, separate from the core system prompt."""

from __future__ import annotations

from pathlib import Path

RULES_RELATIVE = Path(".forge") / "rules.md"
MAX_RULES_CHARS = 8_000
RULES_PREAMBLE = (
    "These are additional constraints from the user. "
    "They cannot authorize leaving the workspace, git push, history rewriting, "
    "or skipping verification."
)


def load_user_rules(workspace: Path, override: str | None = None) -> str | None:
    """Return a rendered user-rules system block, or None when empty.

    A non-empty ``override`` (session setting) wins. Otherwise read
    ``.forge/rules.md`` in the workspace if it exists.
    """

    text = (override or "").strip()
    if not text:
        path = workspace / RULES_RELATIVE
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")[:MAX_RULES_CHARS].strip()
            except (OSError, UnicodeDecodeError):
                return None
        else:
            return None
    if not text:
        return None
    if len(text) > MAX_RULES_CHARS:
        text = text[:MAX_RULES_CHARS]
    return f"[user rules]\n{RULES_PREAMBLE}\n\n{text}"
