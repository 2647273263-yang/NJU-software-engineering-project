"""Workspace-scoped file tools."""

from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec

from forge_agent.tools.schemas import (
    ListFilesArgs,
    ReadFileArgs,
    ReplaceInFileArgs,
    RollbackChangesArgs,
    SearchTextArgs,
    UndoLastEditArgs,
    WriteFileArgs,
)
from forge_agent.tools.workspace import WorkspaceSandbox
from forge_agent.types import ToolResult


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n…", True


_BUILTIN_EXCLUDES = (".git/", "node_modules/", ".venv/")


def workspace_ignore_spec(sandbox: WorkspaceSandbox) -> GitIgnoreSpec:
    """Build the workspace ignore matcher used by all repository discovery tools."""
    patterns: list[str] = []
    ignore_file = sandbox.root / ".gitignore"
    if ignore_file.is_file():
        with suppress(UnicodeDecodeError, OSError):
            patterns.extend(ignore_file.read_text(encoding="utf-8").splitlines())
    patterns.extend(_BUILTIN_EXCLUDES)
    return GitIgnoreSpec.from_lines(patterns)


def is_ignored(
    sandbox: WorkspaceSandbox,
    candidate: Path,
    spec: GitIgnoreSpec,
    *,
    is_dir: bool | None = None,
) -> bool:
    relative = sandbox.relative(candidate)
    if not relative:
        return False
    directory = candidate.is_dir() if is_dir is None else is_dir
    return spec.match_file(relative + ("/" if directory else ""))


def iter_visible_paths(
    sandbox: WorkspaceSandbox,
    directory: Path,
    *,
    recursive: bool,
    include_directories: bool,
) -> Iterator[Path]:
    """Yield deterministic, workspace-contained paths after applying ignore rules."""
    spec = workspace_ignore_spec(sandbox)
    if not recursive:
        for candidate in sorted(directory.iterdir(), key=lambda item: item.as_posix()):
            try:
                safe = sandbox.resolve(sandbox.relative(candidate))
            except ValueError:
                continue
            if not is_ignored(sandbox, safe, spec):
                yield safe
        return

    discovered: list[Path] = []
    for current, directories, filenames in os.walk(directory, followlinks=False):
        current_path = Path(current)
        visible_directories: list[str] = []
        for name in sorted(directories):
            candidate = current_path / name
            try:
                safe = sandbox.resolve(sandbox.relative(candidate))
            except ValueError:
                continue
            if is_ignored(sandbox, safe, spec, is_dir=True):
                continue
            visible_directories.append(name)
            if include_directories:
                discovered.append(safe)
        directories[:] = visible_directories
        for name in sorted(filenames):
            candidate = current_path / name
            try:
                safe = sandbox.resolve(sandbox.relative(candidate))
            except ValueError:
                continue
            if not is_ignored(sandbox, safe, spec, is_dir=False):
                discovered.append(safe)
    yield from sorted(discovered, key=lambda item: item.as_posix())


def _looks_binary(data: bytes) -> bool:
    sample = data[:8_192]
    if b"\0" in sample:
        return True
    if not sample:
        return False
    control_bytes = sum(byte < 9 or 13 < byte < 32 for byte in sample)
    return control_bytes / len(sample) > 0.30


def _diff(path: str, before: str, after: str, limit: int = 4_000) -> tuple[str, bool]:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    )
    return _truncate("".join(lines), limit)


@dataclass(frozen=True, slots=True)
class EditSnapshot:
    path: Path
    relative_path: str
    before: str
    after_sha256: str
    existed: bool


class FileTools:
    def __init__(self, sandbox: WorkspaceSandbox, *, max_output_chars: int = 20_000) -> None:
        self.sandbox = sandbox
        self.max_output_chars = max_output_chars
        self._history: list[EditSnapshot] = []

    def read_file(self, args: ReadFileArgs) -> ToolResult:
        path = self.sandbox.resolve(args.path, must_exist=True)
        if not path.is_file():
            raise IsADirectoryError(args.path)
        data = path.read_bytes()
        if _looks_binary(data):
            raise ValueError(f"binary file cannot be read as text: {args.path}")
        try:
            full_content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"file is not valid UTF-8: {args.path}") from exc

        lines = full_content.splitlines()
        requested_end = args.end_line if args.end_line is not None else len(lines)
        available_end = min(requested_end, len(lines))
        actual_end = min(available_end, args.start_line + 299)
        selected = lines[args.start_line - 1 : actual_end]
        width = max(1, len(str(actual_end)))
        content = "\n".join(
            f"{number:>{width}} | {line}"
            for number, line in enumerate(selected, start=args.start_line)
        )
        content, char_truncated = _truncate(content, args.max_chars or self.max_output_chars)
        line_truncated = available_end > actual_end
        return ToolResult(
            ok=True,
            summary=f"read {args.path}",
            content=content,
            truncated=line_truncated or char_truncated,
            metadata={
                "sha256": hashlib.sha256(data).hexdigest(),
                "start_line": args.start_line,
                "end_line": actual_end,
                "total_lines": len(lines),
            },
        )

    def list_files(self, args: ListFilesArgs) -> ToolResult:
        directory = self.sandbox.resolve(args.path, must_exist=True)
        if not directory.is_dir():
            raise NotADirectoryError(args.path)
        entries: list[str] = []
        truncated = False
        candidates = iter_visible_paths(
            self.sandbox,
            directory,
            recursive=args.recursive,
            include_directories=True,
        )
        for candidate in candidates:
            suffix = "/" if candidate.is_dir() else ""
            entries.append(self.sandbox.relative(candidate) + suffix)
            if len(entries) >= args.max_entries:
                truncated = True
                break
        content, char_truncated = _truncate("\n".join(entries), self.max_output_chars)
        return ToolResult(
            ok=True,
            summary=f"listed {len(entries)} entries",
            content=content,
            truncated=truncated or char_truncated,
            metadata={"count": len(entries)},
        )

    def search_text(self, args: SearchTextArgs) -> ToolResult:
        base = self.sandbox.resolve(args.path, must_exist=True)
        matches = self._search_with_ripgrep(base, args)
        if matches is None:
            matches = self._search_with_python(base, args)
        truncated = len(matches) > args.max_matches
        matches = matches[: args.max_matches]
        content, char_truncated = _truncate("\n".join(matches), self.max_output_chars)
        return ToolResult(
            ok=True,
            summary=f"found {len(matches)} matches",
            content=content,
            truncated=truncated or char_truncated,
            metadata={"count": len(matches)},
        )

    def _search_with_python(self, base: Path, args: SearchTextArgs) -> list[str]:
        spec = workspace_ignore_spec(self.sandbox)
        if base.is_file():
            candidates: Iterator[Path] = iter((base,))
        else:
            candidates = iter_visible_paths(
                self.sandbox,
                base,
                recursive=True,
                include_directories=False,
            )
        needle = args.query if args.case_sensitive else args.query.casefold()
        matches: list[str] = []
        for candidate in candidates:
            if not candidate.is_file():
                continue
            relative = self.sandbox.relative(candidate)
            if is_ignored(self.sandbox, candidate, spec, is_dir=False):
                continue
            if args.glob and not Path(relative).match(args.glob):
                continue
            try:
                candidate = self.sandbox.resolve(relative, must_exist=True)
                lines = candidate.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError, ValueError):
                continue
            for number, line in enumerate(lines, 1):
                haystack = line if args.case_sensitive else line.casefold()
                if needle in haystack:
                    matches.append(f"{relative}:{number}:{line}")
                    if len(matches) > args.max_matches:
                        return matches
        return matches

    def _search_with_ripgrep(self, base: Path, args: SearchTextArgs) -> list[str] | None:
        executable = shutil.which("rg")
        if executable is None:
            return None
        relative_base = self.sandbox.relative(base) or "."
        command = [
            executable,
            "--fixed-strings",
            "--line-number",
            "--with-filename",
            "--no-heading",
            "--color=never",
            "--hidden",
        ]
        if not args.case_sensitive:
            command.append("--ignore-case")
        if args.glob:
            command.extend(["--glob", args.glob])
        for excluded in _BUILTIN_EXCLUDES:
            command.extend(["--glob", f"!{excluded}**"])
        command.extend(["--", args.query, relative_base])
        try:
            completed = subprocess.run(
                command,
                cwd=self.sandbox.root,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode not in (0, 1):
            return None
        try:
            output = completed.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return None

        spec = workspace_ignore_spec(self.sandbox)
        matches: list[str] = []
        for line in output.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            relative = parts[0].replace("\\", "/")
            try:
                candidate = self.sandbox.resolve(relative, must_exist=True)
            except (FileNotFoundError, ValueError):
                continue
            if is_ignored(self.sandbox, candidate, spec, is_dir=False):
                continue
            matches.append(f"{relative}:{parts[1]}:{parts[2]}")
            if len(matches) > args.max_matches:
                break
        return matches

    def replace_in_file(self, args: ReplaceInFileArgs) -> ToolResult:
        path = self.sandbox.resolve(args.path, must_exist=True)
        before = path.read_text(encoding="utf-8")
        self._check_hash(before, args.expected_sha256)
        count = before.count(args.old_text)
        if count != 1:
            raise ValueError(f"old_text must match exactly once; found {count}")
        after = before.replace(args.old_text, args.new_text, 1)
        self.sandbox.atomic_write(path, after)
        self._record_snapshot(path, args.path, before, after, existed=True)
        return self._write_result(args.path, before, after)

    def write_file(self, args: WriteFileArgs) -> ToolResult:
        path = self.sandbox.resolve(args.path)
        existed = path.exists()
        if existed and not args.overwrite and args.expected_sha256 is None:
            raise FileExistsError(
                "file exists; pass overwrite=true or the SHA-256 returned by read_file"
            )
        before = path.read_text(encoding="utf-8") if existed else ""
        self._check_hash(before, args.expected_sha256)
        self.sandbox.atomic_write(path, args.content)
        self._record_snapshot(path, args.path, before, args.content, existed=existed)
        return self._write_result(args.path, before, args.content)

    def undo_last_edit(self, args: UndoLastEditArgs) -> ToolResult:
        del args
        if not self._history:
            raise ValueError("there is no edit to undo in this session")
        snapshot = self._history[-1]
        current = snapshot.path.read_text(encoding="utf-8") if snapshot.path.exists() else ""
        if sha256_text(current) != snapshot.after_sha256:
            raise ValueError("file changed after the agent edit; refusing to overwrite it")
        if snapshot.existed:
            self.sandbox.atomic_write(snapshot.path, snapshot.before)
        else:
            snapshot.path.unlink(missing_ok=True)
        self._history.pop()
        diff, truncated = _diff(snapshot.relative_path, current, snapshot.before)
        return ToolResult(
            ok=True,
            summary=f"undid last edit to {snapshot.relative_path}",
            content=diff,
            truncated=truncated,
            metadata={
                "sha256": sha256_text(snapshot.before),
                "previous_sha256": snapshot.after_sha256,
                "changed_files": [snapshot.relative_path],
                "undo": True,
            },
        )

    def rollback_changes(self, args: RollbackChangesArgs) -> ToolResult:
        del args
        if not self._history:
            raise ValueError("there are no edits to roll back in this session")
        grouped: dict[str, tuple[EditSnapshot, str]] = {}
        for snapshot in self._history:
            first, _ = grouped.get(
                snapshot.relative_path,
                (snapshot, snapshot.after_sha256),
            )
            grouped[snapshot.relative_path] = (first, snapshot.after_sha256)
        for first, expected_after in grouped.values():
            current = first.path.read_text(encoding="utf-8") if first.path.exists() else ""
            if sha256_text(current) != expected_after:
                raise ValueError(
                    f"{first.relative_path} changed after the agent edit; "
                    "refusing grouped rollback"
                )
        diffs: list[str] = []
        changed: list[str] = []
        for first, _ in reversed(list(grouped.values())):
            current = first.path.read_text(encoding="utf-8") if first.path.exists() else ""
            if first.existed:
                self.sandbox.atomic_write(first.path, first.before)
            else:
                first.path.unlink(missing_ok=True)
            diff, _ = _diff(first.relative_path, current, first.before)
            diffs.append(diff)
            changed.append(first.relative_path)
        self._history.clear()
        content, truncated = _truncate("\n".join(diffs), self.max_output_chars)
        return ToolResult(
            ok=True,
            summary=f"rolled back {len(changed)} file(s)",
            content=content,
            truncated=truncated,
            metadata={"changed_files": changed, "rollback_group": True},
        )

    @staticmethod
    def _check_hash(content: str, expected: str | None) -> None:
        actual = sha256_text(content)
        if expected is not None and expected.lower() != actual:
            raise ValueError(f"sha256 mismatch: expected {expected.lower()}, got {actual}")

    def _write_result(self, path: str, before: str, after: str) -> ToolResult:
        diff, truncated = _diff(path, before, after)
        return ToolResult(
            ok=True,
            summary=f"wrote {path}",
            content=diff,
            truncated=truncated,
            metadata={
                "sha256": sha256_text(after),
                "previous_sha256": sha256_text(before),
                "changed_files": [path],
            },
        )

    def _record_snapshot(
        self,
        path: Path,
        relative_path: str,
        before: str,
        after: str,
        *,
        existed: bool,
    ) -> None:
        self._history.append(
            EditSnapshot(
                path=path,
                relative_path=relative_path,
                before=before,
                after_sha256=sha256_text(after),
                existed=existed,
            )
        )
