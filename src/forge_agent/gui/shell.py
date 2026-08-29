"""Persistent interactive PTY for the GUI terminal."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import struct
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from forge_agent.tools.workspace import WorkspaceSandbox

_BUFFER_LIMIT = 200_000


class LiveTerminal:
    """One ConPTY/PTY shell; keystrokes and output are unfiltered."""

    def __init__(
        self,
        workspace: Path,
        *,
        on_idle: Callable[[], None] | None = None,
    ) -> None:
        self.workspace = workspace
        self.buffer = ""
        self._on_idle = on_idle
        self._queues: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()
        self._reader: asyncio.Task[None] | None = None
        self._proc: Any = None
        self._fd: int | None = None
        self._unix_child: asyncio.subprocess.Process | None = None
        self._cols = 80
        self._rows = 24

    async def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            with suppress(Exception):
                if hasattr(proc, "terminate"):
                    proc.terminate(force=True)
                elif hasattr(proc, "kill"):
                    proc.kill()
        child = self._unix_child
        self._unix_child = None
        if child is not None and child.returncode is None:
            with suppress(ProcessLookupError, OSError):
                child.kill()
        reader = self._reader
        self._reader = None
        if reader is not None:
            reader.cancel()
            with suppress(asyncio.CancelledError, TimeoutError, Exception):
                await asyncio.wait_for(reader, timeout=0.8)
        if child is not None:
            with suppress(ProcessLookupError, OSError, TimeoutError):
                await asyncio.wait_for(child.wait(), timeout=0.5)
        fd = self._fd
        self._fd = None
        if fd is not None:
            with suppress(OSError):
                os.close(fd)

    async def ensure(self, cols: int, rows: int) -> None:
        async with self._lock:
            self._cols = max(48, cols)
            self._rows = max(12, rows)
            if self._reader is not None and not self._reader.done():
                self._resize_now(self._cols, self._rows)
                return
            await self._spawn(self._cols, self._rows)
            self._reader = asyncio.create_task(self._read_loop(), name="forge-pty-read")

    async def attach(self, websocket: WebSocket, cols: int, rows: int) -> None:
        await self.ensure(cols, rows)
        queue: asyncio.Queue[str] = asyncio.Queue()
        sender = asyncio.create_task(_pump_queue(queue, websocket), name="forge-pty-ws")
        try:
            if self.buffer:
                await websocket.send_text(self.buffer)
            self._queues.add(queue)
            while True:
                incoming = await websocket.receive_text()
                self._handle_input(incoming)
        except (WebSocketDisconnect, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            sender.cancel()
            with suppress(asyncio.CancelledError):
                await sender
            self._queues.discard(queue)
            if not self._queues:
                await self.close()
                if self._on_idle is not None:
                    self._on_idle()

    def write(self, data: str) -> None:
        if not data:
            return
        if os.name == "nt":
            proc = self._proc
            if proc is None:
                return
            proc.write(data)
            return
        fd = self._fd
        if fd is None:
            return
        os.write(fd, data.encode("utf-8", errors="replace"))

    def _handle_input(self, text: str) -> None:
        if text.startswith("{") and text.endswith("}"):
            with suppress(json.JSONDecodeError, KeyError, TypeError, ValueError):
                payload = json.loads(text)
                if isinstance(payload, dict) and payload.get("type") == "resize":
                    self._cols = max(48, int(payload["cols"]))
                    self._rows = max(12, int(payload["rows"]))
                    self._resize_now(self._cols, self._rows)
                    return
        self.write(text)

    def _broadcast(self, text: str) -> None:
        if not text:
            return
        self.buffer = (self.buffer + text)[-_BUFFER_LIMIT:]
        for queue in list(self._queues):
            with suppress(asyncio.QueueFull):
                queue.put_nowait(text)

    async def _spawn(self, cols: int, rows: int) -> None:
        await self.close()
        if os.name == "nt":
            self._spawn_windows(cols, rows)
            return
        await self._spawn_unix(cols, rows)

    def _spawn_windows(self, cols: int, rows: int) -> None:
        try:
            from winpty import PtyProcess  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError("Windows 交互终端需要 pywinpty") from exc
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        self._proc = PtyProcess.spawn(
            [exe, "-NoLogo", "-NoProfile"],
            cwd=str(self.workspace),
            dimensions=(rows, cols),
            env=env,
        )

    async def _spawn_unix(self, cols: int, rows: int) -> None:
        import pty as posix_pty

        shell = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
        master, slave = posix_pty.openpty()  # type: ignore[attr-defined,unused-ignore]
        self._resize_fd(master, cols, rows)
        self._unix_child = await asyncio.create_subprocess_exec(
            shell,
            "-i",
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=str(self.workspace),
            start_new_session=True,
        )
        os.close(slave)
        self._fd = master

    def _resize_now(self, cols: int, rows: int) -> None:
        if os.name == "nt":
            proc = self._proc
            if proc is not None and hasattr(proc, "setwinsize"):
                with suppress(Exception):
                    proc.setwinsize(rows, cols)
            return
        fd = self._fd
        if fd is not None:
            self._resize_fd(fd, cols, rows)

    def _resize_fd(self, fd: int, cols: int, rows: int) -> None:
        if sys.platform == "win32":
            return
        import fcntl
        import termios

        packed = struct.pack("HHHH", rows, cols, 0, 0)
        with suppress(OSError):
            fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)

    async def _read_loop(self) -> None:
        if os.name == "nt":
            await self._read_windows()
            return
        await self._read_unix()

    async def _read_windows(self) -> None:
        while True:
            proc = self._proc
            if proc is None:
                break
            try:
                chunk = await asyncio.to_thread(proc.read, 4096)
            except (EOFError, OSError):
                break
            if not chunk:
                alive = True
                with suppress(Exception):
                    alive = bool(proc.isalive())
                if not alive:
                    break
                await asyncio.sleep(0.03)
                continue
            text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
            self._broadcast(text)

    async def _read_unix(self) -> None:
        loop = asyncio.get_running_loop()
        fd = self._fd
        if fd is None:
            return
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def on_readable() -> None:
            try:
                data = os.read(fd, 4096)
            except OSError:
                data = b""
            loop.call_soon_threadsafe(queue.put_nowait, data or None)

        loop.add_reader(fd, on_readable)
        try:
            while True:
                chunk = await queue.get()
                if not chunk:
                    break
                self._broadcast(chunk.decode("utf-8", errors="replace"))
        finally:
            with suppress(Exception):
                loop.remove_reader(fd)


async def _pump_queue(queue: asyncio.Queue[str], websocket: WebSocket) -> None:
    try:
        while True:
            chunk = await queue.get()
            await websocket.send_text(chunk)
    except (WebSocketDisconnect, ConnectionResetError, ConnectionAbortedError, OSError, Exception):
        return


def get_terminal(
    shells: dict[str, LiveTerminal],
    workspace: Path,
    session_id: str = "default",
) -> LiveTerminal:
    root = WorkspaceSandbox(workspace).root
    key = f"{root}::{session_id or 'default'}"
    shell = shells.get(key)
    if shell is None:

        def discard() -> None:
            shells.pop(key, None)

        shell = LiveTerminal(root, on_idle=discard)
        shells[key] = shell
    return shell


async def run_terminal_command(
    workspace: Path,
    command: str,
    shells: dict[str, LiveTerminal],
) -> dict[str, Any]:
    """Send one line into the live PTY (used by tests and the REST fallback)."""

    shell = get_terminal(shells, workspace)
    await shell.ensure(80, 24)
    await _wait_for(lambda: ">" in shell.buffer or "$" in shell.buffer, timeout=15.0)
    start = len(shell.buffer)
    ending = "\r" if os.name == "nt" else "\n"
    shell.write(command.strip() + ending)
    await _wait_for(lambda: _command_settled(shell.buffer[start:]), timeout=12.0)
    await asyncio.sleep(0.15)
    body = shell.buffer[start:]
    return {
        "ok": True,
        "content": body,
        "cwd": str(shell.workspace),
        "error_code": None,
    }


def _command_settled(chunk: str) -> bool:
    stripped = chunk.strip()
    if not stripped:
        return False
    if "\n" in chunk and (chunk.rstrip().endswith(">") or chunk.rstrip().endswith("$")):
        return True
    return stripped.count("\n") >= 1 and len(stripped) > 2


async def _wait_for(predicate: Callable[[], bool], timeout: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
