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
  images?: string[];
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
  approve: (id: string, body: { approved: boolean; remember_for_session?: boolean; scope?: "once" | "run" | "session" }) =>
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
  saveAcceptedDiffs: (id: string, diffs: Record<string, string>) =>
    parse<{ accepted_diffs: Record<string, string> }>(
      fetch(`/api/sessions/${id}/accepted-diffs`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ diffs }),
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
      image?: boolean;
      truncated: boolean;
      encoding?: string | null;
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
  deletePath: (workspace: string, path: string) =>
    parse<{ ok: boolean; summary: string; error_code: string | null }>(
      fetch("/api/workspace/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace, path }),
      }),
    ),
  renamePath: (workspace: string, path: string, to: string) =>
    parse<{ ok: boolean; summary: string; error_code: string | null; path?: string }>(
      fetch("/api/workspace/rename", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace, path, to }),
      }),
    ),
  mkdir: (workspace: string, path: string) =>
    parse<{ ok: boolean; summary: string; error_code: string | null; path?: string }>(
      fetch("/api/workspace/mkdir", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace, path }),
      }),
    ),
  uploadImage: (workspace: string, filename: string, dataBase64: string, mime: string) =>
    parse<{ ok: boolean; summary: string; error_code: string | null; path?: string }>(
      fetch("/api/workspace/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          workspace,
          filename,
          data_base64: dataBase64,
          mime,
        }),
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
  gitStatus: (workspace: string) =>
    parse<GitSnapshot>(fetch(`/api/workspace/git?workspace=${encodeURIComponent(workspace)}`)),
  gitInit: (workspace: string) =>
    parse<GitSnapshot>(
      fetch("/api/workspace/git/init", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace }),
      }),
    ),
  gitCommit: (workspace: string, message: string) =>
    parse<GitSnapshot>(
      fetch("/api/workspace/git/commit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace, message }),
      }),
    ),
  gitBranch: (workspace: string, name: string) =>
    parse<GitSnapshot>(
      fetch("/api/workspace/git/branch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace, name }),
      }),
    ),
  gitCheckout: (workspace: string, name: string) =>
    parse<GitSnapshot>(
      fetch("/api/workspace/git/checkout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace, name }),
      }),
    ),
  gitRestore: (workspace: string, commit: string, confirm: boolean) =>
    parse<GitSnapshot>(
      fetch("/api/workspace/git/restore", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace, commit, confirm, clean_untracked: true }),
      }),
    ),
  gitRemote: (workspace: string, url: string) =>
    parse<GitSnapshot>(
      fetch("/api/workspace/git/remote", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace, url }),
      }),
    ),
  gitPush: (workspace: string) =>
    parse<GitSnapshot>(
      fetch("/api/workspace/git/push", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace }),
      }),
    ),
  exportSession: async (id: string) => {
    const response = await fetch(`/api/sessions/${encodeURIComponent(id)}/export`);
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(typeof data.detail === "string" ? data.detail : "导出失败");
    }
    const blob = await response.blob();
    const match = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "");
    const filename = match?.[1] || `forge-session-${id.slice(0, 8)}.json`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  },
  importSession: (payload: unknown) =>
    parse<{ session_id: string }>(
      fetch("/api/sessions/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    ),
};

export type GitSnapshot = {
  available: boolean;
  git_path: string | null;
  repo: boolean;
  reason: string;
  branch: string;
  branches: string[];
  commits: { sha: string; short: string; author: string; when: string; message: string }[];
  dirty: { path: string; kind: string }[];
  remote_url: string;
  branch_nodes: {
    name: string;
    parent: string;
    depth: number;
    current: boolean;
    same_version: boolean;
    ahead: number;
    label: string;
  }[];
};

export type { PendingApproval };
