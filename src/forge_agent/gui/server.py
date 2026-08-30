"""Local FastAPI server: REST + WebSocket around SessionService."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from platformdirs import user_data_path
from pydantic import BaseModel, Field, SecretStr, ValidationError
from starlette.types import ASGIApp

from forge_agent.application import ApplicationEvent, EventBus, SessionService
from forge_agent.config import RunConfig
from forge_agent.gui.shell import LiveTerminal, get_terminal, run_terminal_command
from forge_agent.gui.viewmodels import event_to_view, view_to_dict
from forge_agent.gui.session_bundle import (
    SessionBundleError,
    bundle_filename,
    dump_bundle_json,
    export_session_bundle,
    import_session_bundle,
)
from forge_agent.gui.workspace_git import (
    GitWorkspaceError,
    git_commit,
    git_create_branch,
    git_init,
    git_push,
    git_restore,
    git_set_remote,
    git_snapshot,
    git_switch_branch,
)
from forge_agent.gui.workspace_ops import (
    accepted_diffs_from_metadata,
    apply_session_settings,
    create_workspace_dir,
    create_workspace_file,
    delete_workspace_path,
    image_data_urls,
    latest_diff_for_path,
    pick_directory,
    read_workspace_file,
    rename_workspace_path,
    save_uploaded_image,
    session_settings_from_metadata,
    undo_path,
    workspace_tree,
    write_workspace_file,
)
from forge_agent.model import ModelClient
from forge_agent.privacy import redact_data, redact_text
from forge_agent.storage import SQLiteStorage
from forge_agent.types import RunMode

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_DATABASE = user_data_path("forge-agent", ensure_exists=True) / "sessions.sqlite3"
_FALLBACK_HTML = """<!doctype html>
<html lang="zh-CN">
  <head><meta charset="utf-8"><title>ForgeAgent</title></head>
  <body style="font-family:system-ui;background:#141414;color:#c8c8c8;padding:48px">
    <h1>前端尚未构建</h1>
    <p>在 <code>web/</code> 目录运行 <code>npm install</code> 与 <code>npm run build</code>。</p>
  </body>
</html>
"""


class UTF8JSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=False, default=str).encode("utf-8")


class StartRunBody(BaseModel):
    task: str = Field(min_length=1)
    workspace: str
    model: str = ""
    mode: str = "build"
    verify: str = ""
    max_steps: int = Field(default=30, ge=1, le=100)
    max_tokens: int = Field(default=1_000_000, ge=1_000)
    max_cost: float | None = Field(default=None, gt=0)
    auto_approve: bool = False
    demo: bool = False
    images: list[str] = Field(default_factory=list)


class ResumeBody(BaseModel):
    instruction: str = "继续完成之前的任务。"
    workspace: str = ""
    model: str = ""
    mode: str = "build"
    verify: str = ""
    max_steps: int = Field(default=30, ge=1, le=100)
    max_tokens: int = Field(default=1_000_000, ge=1_000)
    max_cost: float | None = Field(default=None, gt=0)
    auto_approve: bool = False
    demo: bool = False
    images: list[str] = Field(default_factory=list)


class RollbackBody(BaseModel):
    workspace: str = ""


class SessionSettingsBody(BaseModel):
    model: str = ""
    mode: str = ""
    verify: str | None = None
    max_steps: int | None = Field(default=None, ge=1, le=100)
    max_tokens: int | None = Field(default=None, ge=1_000)
    max_cost: float | None = None
    auto_approve: bool | None = None
    demo: bool | None = None
    title: str = ""
    workspace: str = ""


class TerminalBody(BaseModel):
    workspace: str
    command: str = Field(min_length=1)


class SaveFileBody(BaseModel):
    workspace: str
    path: str = Field(min_length=1)
    content: str
    session_id: str = ""
    create: bool = False


class WorkspacePathBody(BaseModel):
    workspace: str
    path: str = Field(min_length=1)


class UploadImageBody(BaseModel):
    workspace: str
    filename: str = ""
    data_base64: str = Field(min_length=1)
    mime: str = ""


class RenamePathBody(BaseModel):
    workspace: str
    path: str = Field(min_length=1)
    to: str = Field(min_length=1)


class UndoBody(BaseModel):
    path: str | None = None
    workspace: str = ""


class AcceptedDiffsBody(BaseModel):
    diffs: dict[str, str] = Field(default_factory=dict)


class GitWorkspaceBody(BaseModel):
    workspace: str


class GitCommitBody(BaseModel):
    workspace: str
    message: str = Field(min_length=1)


class GitBranchBody(BaseModel):
    workspace: str
    name: str = Field(min_length=1)


class GitRestoreBody(BaseModel):
    workspace: str
    commit: str = Field(min_length=7)
    confirm: bool = False
    clean_untracked: bool = True


class GitRemoteBody(BaseModel):
    workspace: str
    url: str = Field(min_length=1)


class ApprovalBody(BaseModel):
    approved: bool
    remember_for_session: bool = False
    scope: Literal["once", "run", "session"] | None = None


@dataclass
class GuiRuntime:
    database_path: Path
    events: EventBus
    service: SessionService
    clients: set[WebSocket] = field(default_factory=set)
    client_demo: dict[WebSocket, bool] = field(default_factory=dict)
    shells: dict[str, LiveTerminal] = field(default_factory=dict)

    async def close_shells(self) -> None:
        shells = list(self.shells.values())
        self.shells.clear()
        if not shells:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(shell.close() for shell in shells), return_exceptions=True),
                timeout=2,
            )

    def workspace_of(self, session_id: str) -> Path | None:
        with SQLiteStorage(self.database_path) as storage:
            session = storage.get_session(session_id)
        if session is None:
            return None
        raw = session.metadata.get("workspace")
        if not raw:
            return None
        return Path(str(raw))

    def redact_workspace(self, session_id: str, *, demo: bool) -> Path | None:
        return self.workspace_of(session_id) if demo else None


def build_run_config(
    *,
    workspace: str,
    model: str,
    mode: str,
    verify: str,
    max_steps: int,
    max_tokens: int,
    max_cost: float | None,
    auto_approve: bool,
    images: list[str] | None = None,
) -> RunConfig:
    api_key = os.environ.get("FORGE_API_KEY")
    if not api_key:
        raise ValueError("尚未在启动 GUI 的终端中配置 FORGE_API_KEY")
    selected_model = model.strip() or os.environ.get("FORGE_MODEL", "")
    if not selected_model:
        raise ValueError("请输入模型名称，或设置 FORGE_MODEL")
    return RunConfig(
        workspace=Path(workspace),
        model=selected_model,
        api_key=SecretStr(api_key),
        base_url=os.environ.get("FORGE_BASE_URL"),
        mode=RunMode(mode),
        verify_command=verify.strip() or None,
        max_steps=max_steps,
        max_total_tokens=max_tokens,
        max_cost_usd=max_cost,
        input_cost_per_million=float(os.environ.get("FORGE_INPUT_COST_PER_MILLION", "0")),
        output_cost_per_million=float(os.environ.get("FORGE_OUTPUT_COST_PER_MILLION", "0")),
        stream_model=True,
        auto_approve=auto_approve,
        user_image_data_urls=image_data_urls(Path(workspace), images or []),
    )


def serialize_event(
    event: ApplicationEvent,
    *,
    workspace: Path | None,
) -> dict[str, Any]:
    payload = redact_data(event.payload, workspace=workspace)
    view = event_to_view(event, workspace=workspace)
    return {
        "session_id": event.session_id,
        "kind": event.kind,
        "payload": payload if isinstance(payload, dict) else {},
        "created_at": event.created_at,
        "view": view_to_dict(view),
    }


def session_rows(
    database_path: Path,
    *,
    demo: bool,
    running_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    active = running_ids or set()
    with SQLiteStorage(database_path) as storage:
        rows = storage.connection.execute(
            "SELECT id, updated_at, metadata_json FROM sessions ORDER BY updated_at DESC LIMIT 200"
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        metadata = json.loads(row["metadata_json"])
        workspace = str(metadata.get("workspace", ""))
        task = str(metadata.get("task", ""))
        label = workspace
        if demo and workspace:
            label = redact_text(workspace, workspace=Path(workspace))
            task = redact_text(task, workspace=Path(workspace))
        session_id = str(row["id"])
        result.append(
            {
                "id": session_id,
                "updated": str(row["updated_at"]),
                "status": str(metadata.get("status", "unknown")),
                "task": task,
                "workspace": workspace,
                "workspace_label": label,
                "mode": str(metadata.get("mode", "build")),
                "running": session_id in active,
            }
        )
    return result


def historical_events(database_path: Path, session_id: str) -> list[ApplicationEvent]:
    with SQLiteStorage(database_path) as storage:
        records = storage.list_events(session_id)
    return [
        ApplicationEvent(
            session_id=session_id,
            kind=record.kind,
            payload=record.payload,
            created_at=record.created_at,
        )
        for record in records
    ]


def claim_rows(
    database_path: Path,
    session_id: str,
    *,
    workspace: Path | None,
) -> list[dict[str, Any]]:
    with SQLiteStorage(database_path) as storage:
        rows: list[dict[str, Any]] = []
        for claim in storage.list_claims(session_id):
            evidence = storage.list_evidence(claim.id)
            description = "\n".join(item.description for item in evidence) or "暂无证据"
            rows.append(
                {
                    "status": claim.status,
                    "statement": redact_text(claim.statement, workspace=workspace),
                    "evidence": redact_text(description, workspace=workspace),
                    "items": [
                        {
                            "kind": item.kind,
                            "description": redact_text(item.description, workspace=workspace),
                            "reference": redact_text(item.reference or "", workspace=workspace),
                        }
                        for item in evidence
                    ],
                }
            )
        return rows


def persist_user_message(
    database_path: Path,
    events: EventBus,
    session_id: str,
    text: str,
    images: list[str] | None = None,
) -> None:
    payload: dict[str, Any] = {"text": text}
    if images:
        payload["images"] = [item for item in images if item]
    with SQLiteStorage(database_path) as storage:
        storage.append_event(session_id, "user_message", payload)
    events.publish(session_id, "user_message", payload)


def persist_gui_settings(database_path: Path, session_id: str, values: dict[str, Any]) -> None:
    with SQLiteStorage(database_path) as storage:
        record = storage.get_session(session_id)
        if record is None:
            return
        updated = apply_session_settings(record.metadata, values)
        changed = {
            key: updated[key]
            for key in (
                "workspace",
                "model",
                "mode",
                "verify_command",
                "max_steps",
                "max_tokens",
                "max_cost",
                "auto_approve",
                "demo",
                "task",
            )
            if record.metadata.get(key) != updated.get(key)
        }
        if changed:
            storage.patch_session_metadata(session_id, changed)


def persist_accepted_diffs(database_path: Path, session_id: str, diffs: dict[str, str]) -> None:
    with SQLiteStorage(database_path) as storage:
        if storage.get_session(session_id) is None:
            return
        storage.patch_session_metadata(session_id, {"accepted_diffs": dict(diffs)})


async def _event_pump(runtime: GuiRuntime, queue: asyncio.Queue[ApplicationEvent]) -> None:
    while True:
        event = await queue.get()
        stale: list[WebSocket] = []
        for websocket in list(runtime.clients):
            demo = runtime.client_demo.get(websocket, False)
            message = serialize_event(
                event,
                workspace=runtime.redact_workspace(event.session_id, demo=demo),
            )
            try:
                await websocket.send_json(message)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            runtime.clients.discard(websocket)


def _silence_win_disconnect(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    exc = context.get("exception")
    if isinstance(exc, ConnectionResetError | ConnectionAbortedError | BrokenPipeError):
        return
    loop.default_exception_handler(context)


def _error(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def create_app(
    *,
    database_path: Path | None = None,
    model_factory: Callable[[RunConfig], ModelClient] | None = None,
) -> FastAPI:
    db_path = database_path or DEFAULT_DATABASE
    events = EventBus()
    runtime = GuiRuntime(
        database_path=db_path,
        events=events,
        service=SessionService(db_path, events=events, model_factory=model_factory),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        queue = runtime.events.subscribe()
        pump = asyncio.create_task(_event_pump(runtime, queue), name="forge-gui-events")
        if sys.platform == "win32":
            asyncio.get_running_loop().set_exception_handler(_silence_win_disconnect)
        try:
            yield
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(pump, timeout=0.5)
            runtime.events.unsubscribe(queue)
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(runtime.close_shells(), timeout=2)

    app = FastAPI(
        title="ForgeAgent",
        default_response_class=UTF8JSONResponse,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8080",
            "http://localhost:8080",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {
            "model": os.environ.get("FORGE_MODEL", ""),
            "has_api_key": bool(os.environ.get("FORGE_API_KEY")),
            "workspace": str(Path.cwd()),
            "base_url_configured": bool(os.environ.get("FORGE_BASE_URL")),
        }

    @app.get("/api/sessions")
    def list_sessions(demo: bool = False) -> dict[str, Any]:
        rows = session_rows(runtime.database_path, demo=demo)
        for item in rows:
            item["running"] = runtime.service.running(str(item["id"])) is not None
        return {"sessions": rows}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str, demo: bool = False) -> dict[str, Any]:
        with SQLiteStorage(runtime.database_path) as storage:
            record = storage.get_session(session_id)
        if record is None:
            raise _error(404, f"unknown session: {session_id}")
        workspace = runtime.redact_workspace(session_id, demo=demo)
        real_workspace = str(record.metadata.get("workspace", ""))
        task = str(record.metadata.get("task", ""))
        if demo and real_workspace:
            task = redact_text(task, workspace=Path(real_workspace))
        events_out = [
            serialize_event(event, workspace=workspace)
            for event in historical_events(runtime.database_path, session_id)
        ]
        pending = []
        for approval in runtime.service.approvals.pending(session_id):
            item = {
                "id": approval.id,
                "kind": approval.kind,
                "tool": approval.call.name,
                "arguments": redact_data(approval.call.arguments, workspace=workspace),
                "risk": approval.decision.risk.value,
                "reason": redact_text(approval.decision.reason, workspace=workspace),
            }
            if approval.kind == "plan":
                item["plan"] = str(approval.call.arguments.get("plan") or "")
                item["arguments"] = {}
            pending.append(item)
        settings = session_settings_from_metadata(record.metadata)
        settings["workspace"] = real_workspace
        return {
            "session": {
                "id": record.id,
                "updated": record.updated_at,
                "status": str(record.metadata.get("status", "unknown")),
                "task": task,
                "workspace": real_workspace,
                "workspace_label": (
                    redact_text(real_workspace, workspace=Path(real_workspace))
                    if demo and real_workspace
                    else real_workspace
                ),
                "mode": str(record.metadata.get("mode", "build")),
                "running": runtime.service.running(session_id) is not None,
            },
            "settings": settings,
            "accepted_diffs": accepted_diffs_from_metadata(record.metadata),
            "events": events_out,
            "claims": claim_rows(runtime.database_path, session_id, workspace=workspace),
            "pending_approvals": pending,
        }

    @app.post("/api/runs")
    async def start_run(body: StartRunBody) -> dict[str, str]:
        try:
            config = build_run_config(
                workspace=body.workspace,
                model=body.model,
                mode=body.mode,
                verify=body.verify,
                max_steps=body.max_steps,
                max_tokens=body.max_tokens,
                max_cost=body.max_cost,
                auto_approve=body.auto_approve,
                images=body.images,
            )
        except (ValueError, ValidationError) as exc:
            raise _error(400, str(exc)) from exc
        running = runtime.service.start_new(config, body.task)
        persist_gui_settings(
            runtime.database_path,
            running.id,
            {
                "model": config.model,
                "mode": body.mode,
                "verify": body.verify,
                "max_steps": body.max_steps,
                "max_tokens": body.max_tokens,
                "max_cost": body.max_cost,
                "auto_approve": body.auto_approve,
                "demo": body.demo,
            },
        )
        persist_user_message(
            runtime.database_path,
            runtime.events,
            running.id,
            body.task,
            body.images,
        )
        return {"session_id": running.id}

    @app.post("/api/sessions/{session_id}/resume")
    async def resume_run(session_id: str, body: ResumeBody) -> dict[str, str]:
        workspace = body.workspace.strip()
        if not workspace:
            stored = runtime.workspace_of(session_id)
            if stored is None:
                raise _error(404, f"unknown session: {session_id}")
            workspace = str(stored)
        try:
            config = build_run_config(
                workspace=workspace,
                model=body.model,
                mode=body.mode,
                verify=body.verify,
                max_steps=body.max_steps,
                max_tokens=body.max_tokens,
                max_cost=body.max_cost,
                auto_approve=body.auto_approve,
                images=body.images,
            )
            running = runtime.service.resume(config, session_id, body.instruction)
        except KeyError as exc:
            raise _error(404, str(exc)) from exc
        except (ValueError, ValidationError, RuntimeError) as exc:
            raise _error(400, str(exc)) from exc
        persist_gui_settings(
            runtime.database_path,
            running.id,
            {
                "model": config.model,
                "mode": body.mode,
                "verify": body.verify,
                "max_steps": body.max_steps,
                "max_tokens": body.max_tokens,
                "max_cost": body.max_cost,
                "auto_approve": body.auto_approve,
                "demo": body.demo,
            },
        )
        persist_user_message(
            runtime.database_path,
            runtime.events,
            running.id,
            body.instruction,
            body.images,
        )
        return {"session_id": running.id}

    @app.post("/api/sessions/{session_id}/cancel")
    def cancel_run(session_id: str) -> dict[str, bool]:
        return {"cancelled": runtime.service.cancel(session_id)}

    @app.post("/api/sessions/{session_id}/rollback")
    async def rollback_run(session_id: str, body: RollbackBody) -> dict[str, Any]:
        workspace = body.workspace.strip()
        if not workspace:
            stored = runtime.workspace_of(session_id)
            if stored is None:
                raise _error(404, f"unknown session: {session_id}")
            workspace = str(stored)
        try:
            config = build_run_config(
                workspace=workspace,
                model="",
                mode="build",
                verify="",
                max_steps=30,
                max_tokens=1_000_000,
                max_cost=None,
                auto_approve=True,
            )
            result = await runtime.service.rollback_changes(config, session_id)
        except KeyError as exc:
            raise _error(404, str(exc)) from exc
        except (ValueError, ValidationError) as exc:
            raise _error(400, str(exc)) from exc
        return {"ok": result.ok, "summary": result.summary, "error_code": result.error_code}

    @app.post("/api/approvals/{approval_id}")
    def resolve_approval(approval_id: str, body: ApprovalBody) -> dict[str, bool]:
        resolved = runtime.service.approvals.resolve(
            approval_id,
            body.approved,
            remember_for_session=body.remember_for_session,
            scope=body.scope,
        )
        if not resolved:
            raise _error(404, f"unknown approval: {approval_id}")
        return {"resolved": True}

    @app.patch("/api/sessions/{session_id}/settings")
    def patch_settings(session_id: str, body: SessionSettingsBody) -> dict[str, Any]:
        values = body.model_dump(exclude_none=True)
        if "verify" not in values and body.verify is None:
            values.pop("verify", None)
        if not str(values.get("workspace") or "").strip():
            values.pop("workspace", None)
        if values.get("workspace") and runtime.service.running(session_id):
            raise _error(400, "运行中不能更换工作区，请先停止当前任务。")
        try:
            persist_gui_settings(runtime.database_path, session_id, values)
        except ValueError as exc:
            raise _error(400, str(exc)) from exc
        with SQLiteStorage(runtime.database_path) as storage:
            record = storage.get_session(session_id)
        if record is None:
            raise _error(404, f"unknown session: {session_id}")
        return {
            "settings": session_settings_from_metadata(record.metadata),
            "task": record.metadata.get("task", ""),
        }

    @app.patch("/api/sessions/{session_id}/accepted-diffs")
    def patch_accepted_diffs(session_id: str, body: AcceptedDiffsBody) -> dict[str, Any]:
        persist_accepted_diffs(runtime.database_path, session_id, body.diffs)
        with SQLiteStorage(runtime.database_path) as storage:
            record = storage.get_session(session_id)
        if record is None:
            raise _error(404, f"unknown session: {session_id}")
        return {"accepted_diffs": accepted_diffs_from_metadata(record.metadata)}

    @app.delete("/api/sessions/{session_id}")
    def remove_session(session_id: str) -> dict[str, bool]:
        runtime.service.cancel(session_id)
        try:
            with SQLiteStorage(runtime.database_path) as storage:
                storage.delete_session(session_id)
        except KeyError as exc:
            raise _error(404, f"unknown session: {session_id}") from exc
        return {"deleted": True}

    @app.get("/api/sessions/{session_id}/export")
    def export_session(session_id: str) -> Response:
        try:
            bundle = export_session_bundle(runtime.database_path, session_id)
        except SessionBundleError as exc:
            raise _error(404, str(exc)) from exc
        filename = bundle_filename(bundle)
        return Response(
            content=dump_bundle_json(bundle),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/sessions/import")
    async def import_session(request: Request) -> dict[str, str]:
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise _error(400, "无法解析 JSON 文件") from exc
        if not isinstance(payload, dict):
            raise _error(400, "文件内容必须是 JSON 对象")
        try:
            session_id = import_session_bundle(runtime.database_path, payload)
        except SessionBundleError as exc:
            raise _error(400, str(exc)) from exc
        return {"session_id": session_id}

    @app.post("/api/workspace/pick")
    async def pick_workspace() -> dict[str, str | None]:
        selected = await asyncio.to_thread(pick_directory)
        return {"path": selected}

    @app.get("/api/workspace/tree")
    def get_tree(workspace: str) -> dict[str, Any]:
        try:
            return {"tree": workspace_tree(Path(workspace))}
        except (ValueError, OSError) as exc:
            raise _error(400, str(exc)) from exc

    @app.get("/api/workspace/file")
    def get_file(workspace: str, path: str, session_id: str = "") -> dict[str, Any]:
        diff = None
        if session_id:
            diff = latest_diff_for_path(historical_events(runtime.database_path, session_id), path)
        try:
            return read_workspace_file(Path(workspace), path, diff=diff)
        except (ValueError, OSError, FileNotFoundError) as exc:
            raise _error(400, str(exc)) from exc

    @app.put("/api/workspace/file")
    async def save_file(body: SaveFileBody) -> dict[str, Any]:
        try:
            if body.create:
                return create_workspace_file(Path(body.workspace), body.path, body.content)
            return await write_workspace_file(
                Path(body.workspace),
                body.path,
                body.content,
                database_path=runtime.database_path,
                session_id=body.session_id,
            )
        except (ValueError, OSError, FileNotFoundError) as exc:
            raise _error(400, str(exc)) from exc

    @app.post("/api/workspace/delete")
    def delete_path(body: WorkspacePathBody) -> dict[str, Any]:
        try:
            return delete_workspace_path(Path(body.workspace), body.path)
        except (ValueError, OSError, FileNotFoundError) as exc:
            raise _error(400, str(exc)) from exc

    @app.post("/api/workspace/mkdir")
    def mkdir_path(body: WorkspacePathBody) -> dict[str, Any]:
        try:
            return create_workspace_dir(Path(body.workspace), body.path)
        except (ValueError, OSError) as exc:
            raise _error(400, str(exc)) from exc

    @app.post("/api/workspace/upload")
    def upload_image(body: UploadImageBody) -> dict[str, Any]:
        try:
            return save_uploaded_image(
                Path(body.workspace),
                body.filename,
                body.data_base64,
                body.mime,
            )
        except (ValueError, OSError) as exc:
            raise _error(400, str(exc)) from exc

    @app.post("/api/workspace/rename")
    def rename_path(body: RenamePathBody) -> dict[str, Any]:
        try:
            return rename_workspace_path(Path(body.workspace), body.path, body.to)
        except (ValueError, OSError, FileNotFoundError) as exc:
            raise _error(400, str(exc)) from exc

    @app.get("/api/workspace/git")
    def get_git(workspace: str) -> dict[str, Any]:
        try:
            return git_snapshot(Path(workspace))
        except (GitWorkspaceError, OSError) as exc:
            raise _error(400, str(exc)) from exc

    @app.post("/api/workspace/git/init")
    def init_git(body: GitWorkspaceBody) -> dict[str, Any]:
        try:
            return git_init(Path(body.workspace))
        except (GitWorkspaceError, OSError) as exc:
            raise _error(400, str(exc)) from exc

    @app.post("/api/workspace/git/commit")
    def commit_git(body: GitCommitBody) -> dict[str, Any]:
        try:
            return git_commit(Path(body.workspace), body.message)
        except (GitWorkspaceError, OSError) as exc:
            raise _error(400, str(exc)) from exc

    @app.post("/api/workspace/git/branch")
    def create_git_branch(body: GitBranchBody) -> dict[str, Any]:
        try:
            return git_create_branch(Path(body.workspace), body.name)
        except (GitWorkspaceError, OSError) as exc:
            raise _error(400, str(exc)) from exc

    @app.post("/api/workspace/git/checkout")
    def checkout_git_branch(body: GitBranchBody) -> dict[str, Any]:
        try:
            return git_switch_branch(Path(body.workspace), body.name)
        except (GitWorkspaceError, OSError) as exc:
            raise _error(400, str(exc)) from exc

    @app.post("/api/workspace/git/restore")
    def restore_git(body: GitRestoreBody) -> dict[str, Any]:
        try:
            return git_restore(
                Path(body.workspace),
                body.commit,
                confirm=body.confirm,
                clean_untracked=body.clean_untracked,
            )
        except (GitWorkspaceError, OSError) as exc:
            raise _error(400, str(exc)) from exc

    @app.post("/api/workspace/git/remote")
    def set_git_remote(body: GitRemoteBody) -> dict[str, Any]:
        try:
            return git_set_remote(Path(body.workspace), body.url)
        except (GitWorkspaceError, OSError) as exc:
            raise _error(400, str(exc)) from exc

    @app.post("/api/workspace/git/push")
    def push_git(body: GitWorkspaceBody) -> dict[str, Any]:
        try:
            return git_push(Path(body.workspace))
        except (GitWorkspaceError, OSError) as exc:
            raise _error(400, str(exc)) from exc

    @app.post("/api/workspace/terminal")
    async def terminal(body: TerminalBody) -> dict[str, Any]:
        try:
            return await run_terminal_command(Path(body.workspace), body.command, runtime.shells)
        except (ValueError, OSError, RuntimeError) as exc:
            raise _error(400, str(exc)) from exc

    @app.websocket("/api/workspace/terminal/ws")
    async def terminal_ws(
        websocket: WebSocket,
        workspace: str,
        cols: int = 80,
        rows: int = 24,
        session: str = "default",
    ) -> None:
        await websocket.accept()
        try:
            shell = get_terminal(runtime.shells, Path(workspace), session)
            await shell.attach(websocket, cols, rows)
        except WebSocketDisconnect:
            return
        except (ValueError, OSError, RuntimeError) as exc:
            with contextlib.suppress(Exception):
                await websocket.send_text(str(exc))

    @app.post("/api/sessions/{session_id}/undo")
    def undo_edit(session_id: str, body: UndoBody) -> dict[str, Any]:
        stored = runtime.workspace_of(session_id)
        workspace = Path(body.workspace or (str(stored) if stored else ""))
        if not workspace:
            raise _error(404, f"unknown session: {session_id}")
        if not body.path:
            return {"ok": False, "summary": "missing path", "error_code": "missing_path"}
        try:
            result = undo_path(runtime.database_path, session_id, workspace, body.path)
        except (ValueError, OSError) as exc:
            raise _error(400, str(exc)) from exc
        return result

    @app.websocket("/api/events")
    async def stream_events(websocket: WebSocket) -> None:
        await websocket.accept()
        runtime.clients.add(websocket)
        runtime.client_demo[websocket] = False
        try:
            while True:
                message = await websocket.receive_json()
                if isinstance(message, dict) and "demo" in message:
                    runtime.client_demo[websocket] = bool(message["demo"])
        except (WebSocketDisconnect, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            runtime.clients.discard(websocket)
            runtime.client_demo.pop(websocket, None)

    assets = STATIC_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", response_model=None)
    def index() -> FileResponse | HTMLResponse:
        index_path = STATIC_DIR / "index.html"
        if index_path.is_file():
            return FileResponse(index_path)
        return HTMLResponse(_FALLBACK_HTML, status_code=503)

    @app.get("/{full_path:path}", response_model=None)
    def spa(full_path: str) -> FileResponse | HTMLResponse:
        if full_path.startswith(("api/", "assets/", "_")):
            raise _error(404, "not found")
        candidate = STATIC_DIR / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        index_path = STATIC_DIR / "index.html"
        if index_path.is_file():
            return FileResponse(index_path)
        return HTMLResponse(_FALLBACK_HTML, status_code=503)

    return app


def as_asgi() -> ASGIApp:
    return create_app()
