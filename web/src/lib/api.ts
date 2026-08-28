import type { PendingApproval, RunSettings, SessionDetail, SessionRow, TreeNode } from "./types";

async function parse<T>(response: Response | Promise<Response>): Promise<T> {
  const resolved = await response;
  const data = await resolved.json().catch(() => ({}));
  if (!resolved.ok) {
    const detail =
      typeof data.detail === "string"
        ? data.detail
        : Array.isArray(data.detail)
          ? data.detail.map((item: { msg?: string }) => item.msg).join("；")
          : resolved.statusText;
    throw new Error(detail || "请求失败");
  }
  return data as T;
}

export type StartRunInput = {
  task: string;
  workspace: string;
  model: string;
  mode: string;
  verify: string;
  max_steps: number;
  max_tokens: number;
  max_cost: number | null;
  auto_approve: boolean;
  demo: boolean;
};

export const api = {
  meta: () => parse<{ model: string; has_api_key: boolean; workspace: string }>(fetch("/api/meta")),
  sessions: (demo: boolean) =>
    parse<{ sessions: SessionRow[] }>(fetch(`/api/sessions?demo=${demo}`)),
  session: (id: string, demo: boolean) =>
    parse<SessionDetail>(fetch(`/api/sessions/${id}?demo=${demo}`)),
  start: (body: StartRunInput) =>
    parse<{ session_id: string }>(
      fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    ),
  resume: (id: string, body: StartRunInput & { instruction: string }) =>
    parse<{ session_id: string }>(
      fetch(`/api/sessions/${id}/resume`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    ),
  cancel: (id: string) =>
    parse<{ cancelled: boolean }>(fetch(`/api/sessions/${id}/cancel`, { method: "POST" })),
  rollback: (id: string, workspace: string) =>
    parse<{ ok: boolean; summary: string }>(
      fetch(`/api/sessions/${id}/rollback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace }),
      }),
    ),
  approve: (id: string, body: { approved: boolean; remember_for_session: boolean }) =>
    parse<{ resolved: boolean }>(
      fetch(`/api/approvals/${id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    ),
  saveSettings: (id: string, settings: Partial<RunSettings>) =>
    parse<{ settings: RunSettings }>(
      fetch(`/api/sessions/${id}/settings`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
      }),
    ),
  pickWorkspace: () => parse<{ path: string | null }>(fetch("/api/workspace/pick", { method: "POST" })),
  tree: (workspace: string) =>
    parse<{ tree: TreeNode[] }>(
      fetch(`/api/workspace/tree?workspace=${encodeURIComponent(workspace)}`),
    ),
  file: (workspace: string, path: string, sessionId?: string) =>
    parse<{
      path: string;
      content: string;
      binary: boolean;
      truncated: boolean;
      diff: string | null;
    }>(
      fetch(
        `/api/workspace/file?workspace=${encodeURIComponent(workspace)}&path=${encodeURIComponent(path)}${
          sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ""
        }`,
      ),
    ),
  saveFile: (workspace: string, path: string, content: string, sessionId?: string) =>
    parse<{ ok: boolean; summary: string; error_code: string | null }>(
      fetch("/api/workspace/file", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          workspace,
          path,
          content,
          session_id: sessionId ?? "",
        }),
      }),
    ),
  createFile: (workspace: string, path: string, content = "") =>
    parse<{ ok: boolean; summary: string; error_code: string | null }>(
      fetch("/api/workspace/file", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace, path, content, create: true }),
      }),
    ),
  deleteSession: (id: string) =>
    parse<{ deleted: boolean }>(fetch(`/api/sessions/${id}`, { method: "DELETE" })),
  renameSession: (id: string, title: string) =>
    parse<{ settings: RunSettings; task?: string }>(
      fetch(`/api/sessions/${id}/settings`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      }),
    ),
  terminal: (workspace: string, command: string) =>
    parse<{ ok: boolean; content: string; cwd: string; error_code: string | null; summary?: string }>(
      fetch("/api/workspace/terminal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace, command }),
      }),
    ),
  undo: (id: string, workspace: string, path: string) =>
    parse<{ ok: boolean; summary: string }>(
      fetch(`/api/sessions/${id}/undo`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace, path }),
      }),
    ),
};

export type { PendingApproval };
