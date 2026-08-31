export type TimelineView = {
  kind: string;
  title: string;
  detail: string;
  tone: string;
  diff: string | null;
  context: Record<string, unknown> | null;
  process: boolean;
  answer: boolean;
  path: string | null;
};

export type SessionEvent = {
  session_id: string;
  kind: string;
  payload: Record<string, unknown>;
  created_at: string;
  view: TimelineView;
};

export type SessionRow = {
  id: string;
  updated: string;
  status: string;
  task: string;
  workspace: string;
  workspace_label?: string;
  mode: string;
  running?: boolean;
};

export type ClaimItem = {
  kind: string;
  description: string;
  reference: string;
};

export type ClaimRow = {
  status: string;
  statement: string;
  evidence: string;
  items?: ClaimItem[];
};

export type PendingApproval = {
  id: string;
  kind?: "tool" | "plan";
  tool: string;
  arguments: Record<string, unknown>;
  risk: string;
  reason: string;
  plan?: string;
};

export type RunSettings = {
  workspace: string;
  model: string;
  mode: "plan" | "build";
  verify: string;
  max_steps: number;
  max_tokens: number;
  max_cost: string;
  auto_approve: boolean;
  demo: boolean;
  extra_rules: string;
};

export type SessionDetail = {
  session: SessionRow;
  settings?: RunSettings;
  accepted_diffs?: Record<string, string>;
  events: SessionEvent[];
  claims: ClaimRow[];
  pending_approvals: PendingApproval[];
};

export type TreeNode = {
  name: string;
  path: string;
  kind: "file" | "dir";
  children: TreeNode[];
};

export const SETTINGS_KEY = "forge-agent-settings";

export function defaultSettings(defaults: Partial<RunSettings> = {}): RunSettings {
  return {
    workspace: defaults.workspace ?? "",
    model: defaults.model ?? "",
    mode: "build",
    verify: "",
    max_steps: 30,
    max_tokens: 1_000_000,
    max_cost: "",
    auto_approve: false,
    demo: false,
    extra_rules: "",
    ...defaults,
  };
}

export function loadSettings(defaults: Partial<RunSettings> = {}): RunSettings {
  const fallback = defaultSettings(defaults);
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as Partial<RunSettings>;
    return defaultSettings({ ...fallback, ...parsed, workspace: "" });
  } catch {
    return fallback;
  }
}

export function saveSettings(settings: RunSettings) {
  const { workspace: _workspace, ...rest } = settings;
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(rest));
}

export const TERMINAL_STATUSES = new Set([
  "completed",
  "failed",
  "cancelled",
  "stopped",
]);

export const STATUS_LABEL: Record<string, string> = {
  initializing: "初始化中",
  thinking: "思考中",
  executing_tool: "执行工具",
  verifying: "验证中",
  awaiting_approval: "等待审批",
  awaiting_plan_approval: "等待批准计划",
  debugging: "调试中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  stopped: "已停止",
  unknown: "未知",
  idle: "空闲",
};

export const RETRY_STATUSES = new Set(["failed", "cancelled", "stopped"]);

export const FILE_WRITE_TOOLS = new Set(["write_file", "replace_in_file", "delete_file"]);

export const HIDDEN_CHAT_KINDS = new Set([
  "model_delta",
  "workspace_summary",
  "context_prepared",
  "context_compacted",
]);
