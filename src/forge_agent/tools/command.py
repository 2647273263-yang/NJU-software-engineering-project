"""Asynchronous subprocess tool with bounded observations."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import shlex
import signal
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Literal

from platformdirs import user_data_path

from forge_agent.privacy import redact_text
from forge_agent.tools.schemas import RunCommandArgs, VerifyChangesArgs
from forge_agent.tools.workspace import WorkspaceSandbox
from forge_agent.types import ToolResult

OutputStream = Literal["stdout", "stderr"]
OutputCallback = Callable[[OutputStream, str], Awaitable[None] | None]

_ALWAYS_INTERACTIVE = {
    "emacs",
    "htop",
    "less",
    "man",
    "more",
    "nano",
    "nvim",
    "top",
    "vi",
    "vim",
}
_SHELLS = {"bash", "cmd", "fish", "powershell", "pwsh", "sh", "zsh"}
_REPLS = {"ipython", "node", "nodejs", "py", "python", "python3", "sqlite3"}
_NON_INTERACTIVE_OPTIONS = {
    "--command",
    "--eval",
    "--file",
    "--help",
    "--module",
    "--version",
    "-c",
    "-e",
    "-m",
    "-v",
}
_OMISSION_MARKER = "\n… omitted …\n"
_BACKGROUND_RE = re.compile(
    r"(?i)(?:^|[;&|]\s*)(?:"
    r"(?:npm|pnpm|yarn|bun)\s+(?:start|run\s+(?:dev|start|serve))|"
    r"(?:python|python3|py)\s+-m\s+http\.server|"
    r"flask\s+run|uvicorn\b|gunicorn\b|daphne\b|"
    r"next\s+dev|\bvite\b|nodemon\b|webpack-dev-server"
    r")"
)


def decode_command_output(data: bytes) -> str:
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if os.name == "nt":
            return data.decode("gbk", errors="replace")
        return data.decode("utf-8", errors="replace")


class CommandTools:
    def __init__(
        self,
        sandbox: WorkspaceSandbox,
        *,
        default_timeout_s: float = 60.0,
        max_output_chars: int = 20_000,
        on_output: OutputCallback | None = None,
        suggested_commands: Sequence[str] = (),
    ) -> None:
        self.sandbox = sandbox
        self.default_timeout_s = default_timeout_s
        self.max_output_chars = max_output_chars
        self.on_output = on_output
        self.suggested_commands = [command for command in suggested_commands if command]

    async def run_command(self, args: RunCommandArgs) -> ToolResult:
        cwd = self.sandbox.resolve(args.cwd, must_exist=True)
        if not cwd.is_dir():
            raise NotADirectoryError(args.cwd)
        interactive_reason = self._interactive_reason(args.command)
        safe_command = redact_text(args.command, workspace=self.sandbox.root)
        if interactive_reason is not None:
            return ToolResult(
                ok=False,
                summary=(
                    "单独输入 python 会打开交互环境，Agent 里不会启动，否则会一直等你打字。"
                    "请改成 `python 某个.py` 或 `python -c ...`；右侧终端可以跑脚本，也可以进 REPL。"
                ),
                error_code="interactive_command",
                metadata={
                    "command": safe_command,
                    "interactive": True,
                    "reason": interactive_reason,
                },
            )
        background_reason = self._background_reason(args.command)
        if background_reason is not None:
            return ToolResult(
                ok=False,
                summary="long-running service command was not started",
                error_code="background_service",
                metadata={
                    "command": safe_command,
                    "background": True,
                    "reason": background_reason,
                },
            )
        kwargs = (
            {"creationflags": getattr(__import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0)}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        process = await asyncio.create_subprocess_shell(
            args.command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        timeout = args.timeout_s or self.default_timeout_s
        timed_out = False
        stdout_task = asyncio.create_task(self._read_stream(process.stdout, "stdout"))
        stderr_task = asyncio.create_task(self._read_stream(process.stderr, "stderr"))
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except TimeoutError:
            timed_out = True
            await self._kill_tree(process)
            await process.wait()
        except asyncio.CancelledError:
            await self._kill_tree(process)
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)

        stdout_text = decode_command_output(stdout)
        stderr_text = decode_command_output(stderr)
        full_content = self._combine_output(stdout_text, stderr_text)
        public_content = redact_text(full_content, workspace=self.sandbox.root)
        truncated = len(full_content) > self.max_output_chars
        log_path: str | None = None
        if truncated:
            log_path = str(self._write_full_output(stdout_text, stderr_text))
            public_content = self._truncate_middle(public_content)
        exit_code = process.returncode if process.returncode is not None else -1
        metadata: dict[str, object] = {
            "command": safe_command,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout_chars": len(stdout_text),
            "stderr_chars": len(stderr_text),
        }
        if log_path is not None:
            metadata["log_path"] = log_path
        return ToolResult(
            ok=not timed_out and exit_code == 0,
            summary="command timed out" if timed_out else f"command exited with code {exit_code}",
            content=public_content,
            truncated=truncated,
            error_code="timeout" if timed_out else (None if exit_code == 0 else "command_failed"),
            metadata=metadata,
        )

    async def verify_changes(self, args: VerifyChangesArgs) -> ToolResult:
        command = args.command or (self.suggested_commands[0] if self.suggested_commands else None)
        if not command:
            return ToolResult(
                ok=False,
                summary="no verification command was provided or inferred",
                error_code="missing_command",
            )
        result = await self.run_command(
            RunCommandArgs(command=command, cwd=args.cwd, timeout_s=args.timeout_s)
        )
        result.metadata["is_verification"] = True
        result.metadata["verification"] = {
            "command": command,
            "exit_code": result.metadata["exit_code"],
            "duration_ms": result.duration_ms,
            "output": result.content,
        }
        return result

    async def _read_stream(
        self,
        stream: asyncio.StreamReader | None,
        name: OutputStream,
    ) -> bytes:
        if stream is None:
            return b""
        chunks: list[bytes] = []
        while chunk := await stream.read(64 * 1024):
            chunks.append(chunk)
            if self.on_output is not None:
                text = redact_text(
                    chunk.decode("utf-8", errors="replace"),
                    workspace=self.sandbox.root,
                )
                with suppress(Exception):
                    callback_result = self.on_output(name, text)
                    if inspect.isawaitable(callback_result):
                        await callback_result
        return b"".join(chunks)

    @staticmethod
    def _combine_output(stdout: str, stderr: str) -> str:
        if not stderr:
            return stdout
        return stdout + ("\n" if stdout and not stdout.endswith("\n") else "") + stderr

    def _truncate_middle(self, content: str) -> str:
        if len(content) <= self.max_output_chars:
            return content
        if self.max_output_chars <= len(_OMISSION_MARKER):
            return _OMISSION_MARKER[: self.max_output_chars]
        available = self.max_output_chars - len(_OMISSION_MARKER)
        head_chars = (available + 1) // 2
        tail_chars = available // 2
        tail = content[-tail_chars:] if tail_chars else ""
        return content[:head_chars] + _OMISSION_MARKER + tail

    def _write_full_output(self, stdout: str, stderr: str) -> Path:
        payload = f"=== stdout ===\n{stdout}\n=== stderr ===\n{stderr}"
        directory: Path | None = None
        candidates = (
            user_data_path("forge-agent") / "command-logs",
            Path(tempfile.gettempdir()) / "forge-agent-command-logs",
            Path.home() / ".forge-agent" / "command-logs",
        )
        for candidate in candidates:
            if candidate.resolve().is_relative_to(self.sandbox.root):
                continue
            with suppress(OSError):
                candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
                with suppress(OSError):
                    candidate.chmod(0o700)
                directory = candidate
                break

        if directory is None:
            raise OSError("no command log directory is available outside the workspace")
        path = directory / f"command-{uuid.uuid4().hex}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(payload)
        except BaseException:
            with suppress(OSError):
                path.unlink()
            raise
        return path.resolve()

    @staticmethod
    def _interactive_reason(command: str) -> str | None:
        for segment in re.split(r"(?:&&|\|\||[;|])", command):
            with suppress(ValueError):
                tokens = shlex.split(segment.strip(), posix=os.name != "nt")
                tokens = [token.strip("\"'") for token in tokens]
                while tokens and (tokens[0].lower() in {"env", "sudo"} or "=" in tokens[0]):
                    tokens.pop(0)
                if not tokens:
                    continue
                executable = Path(tokens[0]).stem.lower()
                arguments = [argument.lower() for argument in tokens[1:]]
                if re.fullmatch(r"(?:i?python|python)(?:\d+(?:\.\d+)*)?", executable):
                    executable = "python"
                if executable in _ALWAYS_INTERACTIVE and not (
                    executable == "top" and "-b" in arguments
                ):
                    return f"{executable} is an interactive terminal program"
                if executable in _SHELLS and (
                    any(argument in {"-i", "--interactive"} for argument in arguments)
                    or not CommandTools._has_shell_command(arguments)
                ):
                    return f"{executable} would open an interactive shell"
                if executable in _REPLS and (
                    any(argument in {"-i", "--interactive"} for argument in arguments)
                    or not CommandTools._has_repl_input(arguments)
                ):
                    return f"{executable} would open an interactive REPL"
                if executable in {"docker", "kubectl", "podman"} and (
                    any(argument in {"--interactive", "--tty"} for argument in arguments)
                    or any(
                        re.fullmatch(r"-[a-z]*[it][a-z]*", argument) for argument in arguments
                    )
                ):
                    return f"{executable} requested an interactive TTY"
                if executable == "ssh" and any(
                    argument == "-t" or re.fullmatch(r"-[a-z]*t[a-z]*", argument)
                    for argument in arguments
                ):
                    return "ssh requested a TTY"
                if executable == "git" and arguments[:1] == ["rebase"] and (
                    "-i" in arguments or "--interactive" in arguments
                ):
                    return "git requested interactive input"
                if (
                    executable == "git"
                    and arguments[:1] in (["add"], ["checkout"], ["reset"], ["restore"], ["stage"])
                    and ("-p" in arguments or "--patch" in arguments)
                ):
                    return "git requested interactive input"
        return None

    @staticmethod
    def _background_reason(command: str) -> str | None:
        match = _BACKGROUND_RE.search(command)
        if match is None:
            return None
        return f"{match.group(0).strip()} looks like a long-running service"

    @staticmethod
    def _has_shell_command(arguments: list[str]) -> bool:
        return any(
            argument in {"-c", "/c", "-command", "-file"}
            or argument.startswith(("-c=", "/c=", "-command:", "-file:"))
            for argument in arguments
        )

    @staticmethod
    def _has_repl_input(arguments: list[str]) -> bool:
        if any(argument in _NON_INTERACTIVE_OPTIONS for argument in arguments):
            return True
        if any(argument.endswith((".py", ".pyw")) for argument in arguments):
            return True
        return any(not argument.startswith("-") for argument in arguments)

    @staticmethod
    async def _kill_tree(process: asyncio.subprocess.Process) -> None:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            killpg = getattr(os, "killpg")  # noqa: B009 - absent from Windows stubs
            sigkill = getattr(signal, "SIGKILL")  # noqa: B009 - absent from Windows stubs
            with suppress(ProcessLookupError):
                killpg(process.pid, sigkill)
        if process.returncode is None:
            process.kill()
