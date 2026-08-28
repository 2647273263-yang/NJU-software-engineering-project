"""Locate the workspace-bundled Git executable. Never writes git config."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def resolve_git_executable() -> str | None:
    """Prefer FORGE_GIT, then workspace MinGit, then PATH."""

    env = os.environ.get("FORGE_GIT", "").strip()
    if env:
        candidate = Path(env)
        if candidate.is_file():
            return str(candidate)

    here = Path(__file__).resolve()
    names = (
        Path("tools") / "MinGit" / "cmd" / "git.exe",
        Path("tools") / "MinGit" / "cmd" / "git",
        Path("tools") / "MinGit" / "mingw64" / "bin" / "git.exe",
    )
    for parent in here.parents:
        for relative in names:
            candidate = parent / relative
            if candidate.is_file():
                return str(candidate)

    found = shutil.which("git")
    return found
