"""Workspace path containment and safe file replacement."""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path


class WorkspaceViolation(ValueError):
    """Raised when a requested path escapes the workspace."""


class WorkspaceSandbox:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError(f"workspace is not a directory: {self.root}")

    def resolve(self, path: str | Path, *, must_exist: bool = False) -> Path:
        candidate = Path(path)
        if candidate.is_absolute():
            raise WorkspaceViolation("absolute paths are not allowed")
        if any(part == ".." for part in candidate.parts):
            raise WorkspaceViolation("parent path components are not allowed")

        resolved = (self.root / candidate).resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceViolation("path escapes workspace") from exc
        if must_exist and not resolved.exists():
            raise FileNotFoundError(path)
        return resolved

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
