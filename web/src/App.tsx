import { FormEvent, KeyboardEvent, ClipboardEvent, DragEvent, useEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowLeftRight,
  AtSign,
  Check,
  ChevronDown,
  FolderOpen,
  PanelLeftClose,
  PanelLeftOpen,
  Paperclip,
  Pencil,
  Plus,
  Loader2,
  RotateCcw,
  Save,
  SendHorizontal,
  Settings,
  Square,
  ShieldAlert,
  Trash2,
  Undo2,
  X,
} from "lucide-react";
import { api } from "./lib/api";
import { formatChatPayload, imagePathsFromText, imagePathsOf, makeChatRef, type ChatRef } from "./lib/chatRefs";
import {
  collectChangedFiles,
  fileFingerprint,
  isChangeAccepted,
} from "./lib/changes";
import { dropBlock, hunkStats, parseChangeBlocks, type ChangeBlock } from "./lib/diffHunks";
import {
  HIDDEN_CHAT_KINDS,
  FILE_WRITE_TOOLS,
  defaultSettings,
  loadSettings,
  saveSettings,
  STATUS_LABEL,
  TERMINAL_STATUSES,
  RETRY_STATUSES,
  type ClaimRow,
  type PendingApproval,
  type RunSettings,
  type SessionEvent,
  type SessionRow,
  type TimelineView,
  type TreeNode,
} from "./lib/types";
import { Button } from "./components/ui/button";
import { CodeEditor, type EditorSelection } from "./components/CodeEditor";
import { EvidencePanel } from "./components/EvidencePanel";
import { FileTree, flattenFiles } from "./components/FileTree";
import { GitPanel } from "./components/GitPanel";
import { InspectorWorkspace, type InspectorHandle, type InspectorPage } from "./components/InspectorWorkspace";
import { ProcessGroup } from "./components/ProcessGroup";
import { SplitHandle } from "./components/SplitHandle";
import { WorkspaceTerminal } from "./components/WorkspaceTerminal";
import { cn, timeAgo } from "./lib/utils";

type ChatBlock =
  | { type: "user"; text: string }
  | { type: "assistant"; text: string; streaming?: boolean }
  | { type: "process"; items: TimelineView[] };

type OpenDoc = {
  path: string;
  content: string;
  draft: string;
  diff: string | null;
  hunks: ChangeBlock[];
  binary: boolean;
  truncated: boolean;
  image?: boolean;
  stale: boolean;
  incoming: string | null;
  missing?: boolean;
};

const LAYOUT_KEY = "forge-agent-layout";

type LayoutState = {
  left: number;
  inspector: number;
  inspectorOnRight: boolean;
  tree: number;
  bottom: number;
  sessionHidden: boolean;
};

function loadLayout(): LayoutState {
  const fallback: LayoutState = {
    left: 240,
    inspector: 520,
    inspectorOnRight: true,
    tree: 168,
    bottom: 200,
    sessionHidden: false,
  };
  try {
    const raw = localStorage.getItem(LAYOUT_KEY);
    if (!raw) return fallback;
    return { ...fallback, ...JSON.parse(raw) };
  } catch {
    return fallback;
  }
}

function saveLayout(layout: LayoutState) {
  localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function groupEvents(events: SessionEvent[], streamText: string): ChatBlock[] {
  const blocks: ChatBlock[] = [];
  for (const event of events) {
    const view = event.view;
    if (HIDDEN_CHAT_KINDS.has(view.kind)) continue;
    if (view.kind === "run_finished" && event.payload.status === "completed") continue;
    if (view.kind === "user_message") {
      blocks.push({ type: "user", text: view.detail });
      continue;
    }
    if (view.answer) {
      blocks.push({ type: "assistant", text: view.detail });
      continue;
    }
    const last = blocks[blocks.length - 1];
    if (last?.type === "process") last.items.push(view);
    else blocks.push({ type: "process", items: [view] });
  }
  if (streamText) {
    const last = blocks[blocks.length - 1];
    if (last?.type === "assistant" && last.streaming) last.text = streamText;
    else blocks.push({ type: "assistant", text: streamText, streaming: true });
  }
  return blocks;
}

function costValue(raw: string): number | null {
  const value = Number(raw);
  return raw.trim() && value > 0 ? value : null;
}

function shortPath(path: string): string {
  const parts = path.replaceAll("\\", "/").split("/").filter(Boolean);
  return parts.slice(-2).join("/") || path || "未选择工作区";
}


function ChangeKindLabel({ kind }: { kind: "added" | "deleted" | "modified" }) {
  if (kind === "added") {
    return <span className="shrink-0 text-[11px] text-emerald-400">新增</span>;
  }
  if (kind === "deleted") {
    return <span className="shrink-0 text-[11px] text-red-400">已删除</span>;
  }
  return null;
}

function LineDelta({ added, deleted }: { added: number; deleted: number }) {
  if (added === 0 && deleted === 0) return null;
  return (
    <span className="shrink-0 font-mono text-[12px] tabular-nums">
      <span className="text-emerald-400">+{added}</span>
      <span className="ml-1.5 text-red-400">-{deleted}</span>
    </span>
  );
}

function ModeSelect({
  value,
  onChange,
}: {
  value: RunSettings["mode"];
  onChange: (mode: RunSettings["mode"]) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <button
        type="button"
        className="flex h-7 items-center gap-1 rounded-md bg-white/[0.05] px-2 text-[12px] text-foreground transition-colors duration-150 hover:bg-white/[0.08]"
        onClick={() => setOpen((current) => !current)}
      >
        {value === "build" ? "Agent" : "Plan"}
        <ChevronDown className="h-3 w-3" />
      </button>
      {open ? (
        <div className="absolute bottom-8 left-0 z-20 min-w-[88px] overflow-hidden rounded-md bg-popover py-1 ring-1 ring-white/[0.08]">
          {(
            [
              ["build", "Agent"],
              ["plan", "Plan"],
            ] as const
          ).map(([mode, label]) => (
            <button
              key={mode}
              type="button"
              className={cn(
                "block w-full px-3 py-1.5 text-left text-[12px] text-foreground transition-colors duration-150 hover:bg-white/[0.06]",
                value === mode && "bg-primary/15 text-primary",
              )}
              onClick={() => {
                onChange(mode);
                setOpen(false);
              }}
            >
              {label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function withRunEnded(sessions: SessionRow[], sessionId: string, status: string): SessionRow[] {
  return sessions.map((session) =>
    session.id === sessionId ? { ...session, running: false, status } : session,
  );
}

function sessionShowsSpinner(
  session: SessionRow,
  selectedId: string | null,
  running: boolean,
  selectedStatus: string,
): boolean {
  const live = Boolean(session.running) || (session.id === selectedId && running);
  if (!live) return false;
  const current = session.id === selectedId ? selectedStatus : session.status;
  return !TERMINAL_STATUSES.has(current);
}

export default function App() {
  const [settings, setSettings] = useState<RunSettings>(() => loadSettings());
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [claims, setClaims] = useState<ClaimRow[]>([]);
  const [approval, setApproval] = useState<PendingApproval | null>(null);
  const [task, setTask] = useState("");
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState("idle");
  const [streamText, setStreamText] = useState("");
  const [error, setError] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [hasKey, setHasKey] = useState(true);
  const [filesOpen, setFilesOpen] = useState(true);
  const [layout, setLayout] = useState<LayoutState>(loadLayout);
  const layoutBox = useRef<HTMLDivElement>(null);
  const filesPane = useRef<HTMLDivElement>(null);
  const inspectorBox = useRef<HTMLDivElement>(null);
  const inspector = useRef<InspectorHandle>(null);
  const importFileRef = useRef<HTMLInputElement>(null);
  const attachInputRef = useRef<HTMLInputElement>(null);
  const dropDepth = useRef(0);
  const layoutRef = useRef(layout);
  layoutRef.current = layout;
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [tabs, setTabs] = useState<OpenDoc[]>([]);
  const [activePath, setActivePath] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteQuery, setPaletteQuery] = useState("");
  const [paletteMode, setPaletteMode] = useState<"open" | "insert">("open");
  const [chatRefs, setChatRefs] = useState<ChatRef[]>([]);
  const [dropActive, setDropActive] = useState(false);
  const [resolvedDiffs, setResolvedDiffs] = useState<Record<string, string>>({});
  const scroller = useRef<HTMLDivElement>(null);
  const selectedRef = useRef<string | null>(null);
  selectedRef.current = selectedId;
  const runningRef = useRef(false);
  runningRef.current = running;
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  const resolvedRef = useRef(resolvedDiffs);
  resolvedRef.current = resolvedDiffs;
  const tabsRef = useRef(tabs);
  tabsRef.current = tabs;

  const blocks = useMemo(() => groupEvents(events, streamText), [events, streamText]);
  const changed = useMemo(() => {
    try {
      return collectChangedFiles(events);
    } catch {
      return [];
    }
  }, [events]);
  const changedRef = useRef(changed);
  changedRef.current = changed;
  const pendingChanges = useMemo(
    () => changed.filter((file) => !isChangeAccepted(file, resolvedDiffs)),
    [changed, resolvedDiffs],
  );
  const changedPaths = useMemo(() => new Set(pendingChanges.map((file) => file.path)), [pendingChanges]);
  const addedTotal = pendingChanges.reduce((sum, file) => sum + file.added, 0);
  const deletedTotal = pendingChanges.reduce((sum, file) => sum + file.deleted, 0);
  const openFile = tabs.find((item) => item.path === activePath) ?? null;
  const draft = openFile?.draft ?? "";
  const dirty = Boolean(
    openFile && !openFile.binary && !openFile.truncated && openFile.draft !== openFile.content,
  );
  const anyDirty = tabs.some((item) => !item.binary && item.draft !== item.content);
  const context =
    [...events].reverse().map((event) => event.view.context).find((item) => item) ?? null;
  const paletteFiles = useMemo(() => {
    const all = flattenFiles(tree);
    const q = paletteQuery.trim().toLowerCase();
    return q ? all.filter((path) => path.toLowerCase().includes(q)) : all.slice(0, 40);
  }, [tree, paletteQuery]);
  const lastUserText = useMemo(() => {
    for (let index = events.length - 1; index >= 0; index -= 1) {
      if (events[index]?.view.kind === "user_message") return events[index].view.detail;
    }
    return "";
  }, [events]);
  const lastUserImages = useMemo(() => {
    for (let index = events.length - 1; index >= 0; index -= 1) {
      if (events[index]?.view.kind !== "user_message") continue;
      const raw = events[index].payload.images;
      if (Array.isArray(raw)) return raw.filter((item): item is string => typeof item === "string");
      return [];
    }
    return [] as string[];
  }, [events]);
  const liveHint = useMemo(() => {
    if (!running) return "";
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const view = events[index]?.view;
      if (view?.kind === "tool_started") return `正在${view.title}`;
      if (view?.kind === "approval_requested") return view.title;
    }
    return STATUS_LABEL[status] ?? "运行中";
  }, [events, running, status]);
  const canRetry = Boolean(selectedId && !running && lastUserText && RETRY_STATUSES.has(status));

  function persistResolved(sessionId: string | null, value: Record<string, string>) {
    if (!sessionId) return;
    try {
      localStorage.setItem(`forge-resolved:${sessionId}`, JSON.stringify(value));
    } catch {
      /* ignore quota / private mode */
    }
    void api.saveAcceptedDiffs(sessionId, value).catch(() => undefined);
  }

  function readResolved(sessionId: string): Record<string, string> {
    try {
      const local = localStorage.getItem(`forge-resolved:${sessionId}`);
      if (local) {
        return JSON.parse(local) as Record<string, string>;
      }
      const previous = sessionStorage.getItem(`forge-resolved:${sessionId}`);
      if (previous) {
        localStorage.setItem(`forge-resolved:${sessionId}`, previous);
        return JSON.parse(previous) as Record<string, string>;
      }
      return {};
    } catch {
      return {};
    }
  }

  function setDraft(next: string) {
    setTabs((current) =>
      current.map((item) => (item.path === activePath ? { ...item, draft: next } : item)),
    );
  }

  async function refreshSessions() {
    const data = await api.sessions(settingsRef.current.demo);
    setSessions(data.sessions);
  }

  async function loadSession(id: string) {
    if (anyDirty && !window.confirm("切换会话将丢弃未保存的修改，确定吗？")) return;
    const data = await api.session(id, settingsRef.current.demo);
    setSelectedId(id);
    setEvents(data.events);
    setClaims(data.claims);
    setApproval(data.pending_approvals[0] ?? null);
    setStatus(data.session.status);
    setRunning(Boolean(data.session.running) && !TERMINAL_STATUSES.has(data.session.status));
    setStreamText("");
    setTask("");
    setChatRefs([]);
    setTabs([]);
    setActivePath(null);
    setResolvedDiffs({ ...readResolved(id), ...(data.accepted_diffs ?? {}) });
    inspector.current?.open("files");
    if (data.settings) {
      const next = defaultSettings(data.settings);
      setSettings(next);
      saveSettings(next);
    }
  }

  async function refreshTree(workspace = settings.workspace) {
    if (!workspace) {
      setTree([]);
      return;
    }
    try {
      const data = await api.tree(workspace);
      setTree(data.tree);
    } catch {
      setTree([]);
    }
  }

  async function refreshAfterGit() {
    await refreshTree();
    for (const tab of tabsRef.current) {
      await syncTabFromDisk(tab.path);
    }
  }

  async function openWorkspaceFile(path: string) {
    if (!settings.workspace) return;
    inspector.current?.open("files");
    const existing = tabsRef.current.find((item) => item.path === path);
    if (existing && !existing.missing) {
      setActivePath(path);
      return;
    }
    try {
      const data = await api.file(settings.workspace, path, selectedId ?? undefined);
      const file = changedRef.current.find((item) => item.path === data.path || item.path === path);
      const hidden = !file || isChangeAccepted(file, resolvedRef.current);
      const doc: OpenDoc = {
        path: data.path,
        content: data.content,
        draft: data.content,
        diff: hidden ? null : (data.diff ?? file?.diff ?? null),
        hunks: hidden ? [] : parseChangeBlocks(data.diff ?? file?.diff ?? null),
        binary: data.binary,
        image: Boolean(data.image),
        truncated: data.truncated,
        stale: false,
        incoming: null,
        missing: false,
      };
      setTabs((current) => [...current.filter((item) => item.path !== path), doc]);
      setActivePath(path);
    } catch {
      const pending = changedRef.current.find((item) => item.path === path);
      const hidden = !pending || isChangeAccepted(pending, resolvedRef.current);
      if (pending?.kind === "deleted" && !hidden) {
        const doc: OpenDoc = {
          path,
          content: "",
          draft: "",
          diff: pending.diff,
          hunks: parseChangeBlocks(pending.diff),
          binary: false,
          image: false,
          truncated: false,
          stale: false,
          incoming: null,
          missing: true,
        };
        setTabs((current) => [...current.filter((item) => item.path !== path), doc]);
        setActivePath(path);
        return;
      }
      setError(`无法打开 ${path}`);
    }
  }

  async function closeTab(path: string) {
    const doc = tabsRef.current.find((item) => item.path === path);
    if (doc && doc.draft !== doc.content && !window.confirm("关闭标签将丢弃未保存修改，确定吗？")) return;
    const remaining = tabsRef.current.filter((item) => item.path !== path);
    setTabs(remaining);
    if (activePath === path) setActivePath(remaining[remaining.length - 1]?.path ?? null);
  }

  async function saveOpenFile(force = false) {
    if (!openFile || !settings.workspace || openFile.binary || openFile.truncated) return;
    if (!force && openFile.draft === openFile.content) return;
    if (openFile.stale && !window.confirm("磁盘上有更新的内容。保存将覆盖磁盘上的版本，确定吗？")) return;
    setSaving(true);
    try {
      const result = await api.saveFile(
        settings.workspace,
        openFile.path,
        openFile.draft,
        selectedId ?? undefined,
      );
      if (!result.ok) {
        setError(result.summary);
        return;
      }
      setTabs((current) =>
        current.map((item) =>
          item.path === openFile.path
            ? { ...item, content: item.draft, stale: false, incoming: null }
            : item,
        ),
      );
      setError("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function syncTabFromDisk(path: string) {
    if (!settingsRef.current.workspace) return;
    if (!tabsRef.current.some((item) => item.path === path)) return;
    try {
      const data = await api.file(settingsRef.current.workspace, path, selectedRef.current ?? undefined);
      setTabs((current) =>
        current.map((item) => {
          if (item.path !== path) return item;
          if (item.draft === item.content) {
            const file = changedRef.current.find((entry) => entry.path === data.path || entry.path === path);
            const hidden = !file || isChangeAccepted(file, resolvedRef.current);
            return {
              ...item,
              content: data.content,
              draft: data.content,
              binary: data.binary,
              image: Boolean(data.image),
              truncated: data.truncated,
              diff: hidden ? null : data.diff,
              hunks: hidden ? [] : parseChangeBlocks(data.diff),
              stale: false,
              incoming: null,
              missing: false,
            };
          }
          if (data.content !== item.content) {
            return { ...item, stale: true, incoming: data.content };
          }
          return item;
        }),
      );
    } catch {
      const pending = changedRef.current.find((item) => item.path === path);
      if (pending?.kind !== "deleted") return;
      if (isChangeAccepted(pending, resolvedRef.current)) return;
      setTabs((current) =>
        current.map((item) =>
          item.path === path
            ? {
                ...item,
                content: "",
                draft: "",
                diff: pending.diff,
                hunks: parseChangeBlocks(pending.diff),
                stale: false,
                incoming: null,
                missing: true,
              }
            : item,
        ),
      );
    }
  }

  useEffect(() => {
    const tick = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(tick);
  }, []);

  useEffect(() => {
    let closed = false;
    (async () => {
      try {
        const meta = await api.meta();
        setHasKey(meta.has_api_key);
        setSettings((current) => {
          const next = defaultSettings({
            ...current,
            model: current.model || meta.model,
          });
          saveSettings(next);
          return next;
        });
        await refreshSessions();
      } catch (exc) {
        if (!closed) setError(exc instanceof Error ? exc.message : "无法连接本地服务");
      }
    })();
    return () => {
      closed = true;
    };
  }, []);

  useEffect(() => {
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${location.host}/api/events`);
    socket.addEventListener("open", () => {
      socket.send(JSON.stringify({ demo: settings.demo }));
    });
    socket.addEventListener("message", (message) => {
      const event = JSON.parse(message.data) as SessionEvent;
      if (event.kind === "run_finished") {
        const nextStatus = String(event.payload.status ?? "completed");
        if (event.session_id === selectedRef.current) {
          setRunning(false);
          setStreamText("");
          setStatus(nextStatus);
          void refreshTree(settingsRef.current.workspace);
          void api.session(event.session_id, settingsRef.current.demo).then((data) => {
            setClaims(data.claims);
            setEvents(data.events);
            setApproval(data.pending_approvals[0] ?? null);
            setResolvedDiffs((current) => ({
              ...current,
              ...readResolved(event.session_id),
              ...(data.accepted_diffs ?? {}),
            }));
          });
        }
        setSessions((current) => withRunEnded(current, event.session_id, nextStatus));
        void refreshSessions().then(() => {
          setSessions((current) =>
            current.map((session) => {
              if (session.id !== event.session_id) return session;
              if (session.id === selectedRef.current && runningRef.current) return session;
              return { ...session, running: false, status: nextStatus };
            }),
          );
        });
      }
      if (event.session_id !== selectedRef.current) {
        if (event.kind !== "run_finished") void refreshSessions();
        return;
      }
      if (event.kind === "model_delta") {
        setStreamText((text) => text + String(event.payload.text ?? event.view.detail));
        return;
      }
      if (event.kind === "model_response") setStreamText("");
      setEvents((current) => {
        if (event.kind === "user_message") {
          const last = current[current.length - 1];
          if (last?.kind === "user_message" && last.view.detail === event.view.detail) {
            return current;
          }
        }
        return [...current, event];
      });
      if (event.kind === "approval_requested") {
        setApproval({
          id: String(event.payload.approval_id ?? ""),
          kind: event.payload.kind === "plan" ? "plan" : "tool",
          tool: String(event.payload.tool ?? ""),
          arguments: (event.payload.arguments as Record<string, unknown>) ?? {},
          risk: String(event.payload.risk ?? ""),
          reason: String(event.payload.reason ?? ""),
          plan: typeof event.payload.plan === "string" ? event.payload.plan : undefined,
        });
        setStatus(
          event.payload.kind === "plan" ? "awaiting_plan_approval" : "awaiting_approval",
        );
      }
      if (event.kind === "approval_resolved") setApproval(null);
      if (event.view.path) void syncTabFromDisk(event.view.path.replaceAll("\\", "/"));
      if (event.view.diff || event.view.path) void refreshTree(settingsRef.current.workspace);
    });
    return () => socket.close();
  }, [settings.demo]);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [blocks, streamText]);

  useEffect(() => {
    void refreshTree();
  }, [settings.workspace]);

  useEffect(() => {
    setTabs((current) =>
      current.map((doc) => {
        const file = changed.find((item) => item.path === doc.path);
        const hidden = !file || isChangeAccepted(file, resolvedDiffs);
        const nextDiff = hidden ? null : (file?.diff ?? null);
        if (doc.diff === nextDiff) return doc;
        return { ...doc, diff: nextDiff, hunks: parseChangeBlocks(nextDiff) };
      }),
    );
  }, [changed, resolvedDiffs]);

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "p") {
        event.preventDefault();
        setPaletteMode("open");
        setPaletteOpen(true);
        setPaletteQuery("");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    const blockNavigation = (event: globalThis.DragEvent) => {
      if (Array.from(event.dataTransfer?.types ?? []).includes("Files")) {
        event.preventDefault();
      }
    };
    window.addEventListener("dragover", blockNavigation);
    window.addEventListener("drop", blockNavigation);
    return () => {
      window.removeEventListener("dragover", blockNavigation);
      window.removeEventListener("drop", blockNavigation);
    };
  }, []);

  useEffect(() => {
    const onUnload = (event: BeforeUnloadEvent) => {
      if (!anyDirty) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, [anyDirty]);

  function runPayload(text: string, images: string[] = []) {
    return {
      task: text,
      instruction: text,
      workspace: settings.workspace,
      model: settings.model,
      mode: settings.mode,
      verify: settings.verify,
      max_steps: settings.max_steps,
      max_tokens: settings.max_tokens,
      max_cost: costValue(settings.max_cost),
      auto_approve: settings.auto_approve,
      demo: settings.demo,
      images,
    };
  }

  async function submit(event?: FormEvent, rawText?: string, images?: string[]) {
    event?.preventDefault();
    const attached = images ?? imagePathsOf(chatRefs);
    const text = (rawText ?? formatChatPayload(task, chatRefs)).trim();
    if (!text || running) return;
    if (!settings.workspace) {
      setError("请先选择工作区文件夹。");
      return;
    }
    setError("");
    const userEvent: SessionEvent = {
      session_id: selectedId ?? "pending",
      kind: "user_message",
      payload: { text, images: attached },
      created_at: new Date().toISOString(),
      view: {
        kind: "user_message",
        title: "用户",
        detail: text,
        tone: "neutral",
        diff: null,
        context: null,
        process: false,
        answer: false,
        path: null,
      },
    };
    try {
      if (selectedId && events.length > 0) {
        setEvents((current) => [...current, userEvent]);
        setRunning(true);
        setStatus("initializing");
        setTask("");
        setChatRefs([]);
        await api.resume(selectedId, runPayload(text, attached));
      } else {
        setEvents([userEvent]);
        setClaims([]);
        setStreamText("");
        setRunning(true);
        setStatus("initializing");
        setTask("");
        setChatRefs([]);
        const result = await api.start(runPayload(text, attached));
        setSelectedId(result.session_id);
      }
      await refreshSessions();
    } catch (exc) {
      setRunning(false);
      setStatus("failed");
      setError(exc instanceof Error ? exc.message : "启动失败");
    }
  }

  function onComposerKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "@") {
      const start = event.currentTarget.selectionStart ?? 0;
      const before = start > 0 ? event.currentTarget.value[start - 1] : "";
      if (start === 0 || before === " " || before === "\n") {
        event.preventDefault();
        setPaletteMode("insert");
        setPaletteOpen(true);
        setPaletteQuery("");
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }

  function addChatRef(ref: ChatRef) {
    setChatRefs((current) => [...current.filter((item) => item.id !== ref.id), ref]);
  }

  function insertFileRef(path: string) {
    addChatRef(makeChatRef(path));
  }

  function askSelection(selection: EditorSelection) {
    if (!openFile) return;
    addChatRef(
      makeChatRef(openFile.path, {
        startLine: selection.startLine,
        endLine: selection.endLine,
        snippet: selection.text,
      }),
    );
  }

  async function retryLast() {
    if (!canRetry) return;
    await submit(
      undefined,
      lastUserText,
      lastUserImages.length > 0 ? lastUserImages : imagePathsFromText(lastUserText),
    );
  }

  function fileToDataUrl(file: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("无法读取文件"));
      reader.readAsDataURL(file);
    });
  }

  function isDirectoryDrop(event: DragEvent<HTMLElement>): boolean {
    const items = event.dataTransfer?.items;
    if (!items) return false;
    for (let index = 0; index < items.length; index += 1) {
      const item = items[index] as DataTransferItem & {
        webkitGetAsEntry?: () => { isDirectory: boolean } | null;
      };
      if (item.webkitGetAsEntry?.()?.isDirectory) return true;
    }
    return false;
  }

  async function ingestFiles(files: File[]) {
    if (!settings.workspace) {
      setError("请先选择工作区");
      return;
    }
    const nested = files.some((file) => /[\\/]/.test(file.webkitRelativePath || ""));
    if (nested) {
      setError("请拖单个文件，不要拖文件夹");
      return;
    }
    if (files.length === 0) {
      setError("请拖单个文件");
      return;
    }
    const notes: string[] = [];
    const selected = files.slice(0, 5);
    if (files.length > 5) notes.push("一次最多附上 5 个文件，已忽略多余的。");
    for (const file of selected) {
      try {
        const dataUrl = await fileToDataUrl(file);
        const result = await api.uploadFile(settings.workspace, file.name || "file", dataUrl, file.type);
        if (!result.ok || !result.path) {
          notes.push(result.summary || `${file.name} 无法附上`);
          continue;
        }
        addChatRef(
          makeChatRef(result.path, {
            kind: result.kind === "image" ? "image" : "file",
            preview: result.kind === "image" ? dataUrl : undefined,
            label: file.name || undefined,
          }),
        );
      } catch (exc) {
        notes.push(exc instanceof Error ? exc.message : `${file.name} 无法附上`);
      }
    }
    setError(notes.join(" "));
  }

  async function onComposerPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const listed = Array.from(event.clipboardData?.files ?? []);
    if (listed.length === 0) {
      const imageItem = Array.from(event.clipboardData?.items ?? []).find((item) =>
        item.type.startsWith("image/"),
      );
      const file = imageItem?.getAsFile();
      if (file) listed.push(file);
    }
    if (listed.length === 0) return;
    event.preventDefault();
    await ingestFiles(listed);
  }

  function onComposerDragOver(event: DragEvent<HTMLElement>) {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  }

  function onComposerDragEnter(event: DragEvent<HTMLElement>) {
    event.preventDefault();
    dropDepth.current += 1;
    setDropActive(true);
  }

  function onComposerDragLeave() {
    dropDepth.current = Math.max(0, dropDepth.current - 1);
    if (dropDepth.current === 0) setDropActive(false);
  }

  async function onComposerDrop(event: DragEvent<HTMLElement>) {
    event.preventDefault();
    dropDepth.current = 0;
    setDropActive(false);
    if (isDirectoryDrop(event)) {
      setError("请拖单个文件，不要拖文件夹");
      return;
    }
    await ingestFiles(Array.from(event.dataTransfer?.files ?? []));
  }

  async function changeWorkspace() {
    if (running) {
      setError("运行中不能更换工作区，请先停止当前任务。");
      return;
    }
    try {
      const picked = await api.pickWorkspace();
      if (!picked.path) return;
      if (picked.path.replaceAll("\\", "/") === settings.workspace.replaceAll("\\", "/")) return;
      if (selectedId) {
        if (
          !window.confirm(
            "更换工作区后，本会话将针对新文件夹继续。此前针对旧文件夹的撤销快照可能不再适用。确定吗？",
          )
        ) {
          return;
        }
        await api.saveSettings(selectedId, { workspace: picked.path });
      }
      setTabs([]);
      setActivePath(null);
      setError("");
      const next = defaultSettings({ ...settings, workspace: picked.path });
      setSettings(next);
      saveSettings(next);
      await refreshTree(picked.path);
      await refreshSessions();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "无法更换工作区");
    }
  }

  async function newChat() {
    if (running) return;
    try {
      const picked = await api.pickWorkspace();
      if (!picked.path) return;
      setSelectedId(null);
      setEvents([]);
      setClaims([]);
      setApproval(null);
      setStreamText("");
      setTask("");
      setChatRefs([]);
      setStatus("idle");
      setError("");
      setTabs([]);
      setActivePath(null);
      setResolvedDiffs({});
      inspector.current?.open("files");
      const next = defaultSettings({ ...settings, workspace: picked.path });
      setSettings(next);
      saveSettings(next);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "无法打开文件夹对话框");
    }
  }

  function rememberAccepted(path: string, fingerprint: string) {
    const next = { ...resolvedRef.current, [path]: fingerprint };
    setResolvedDiffs(next);
    persistResolved(selectedRef.current, next);
    setTabs((current) =>
      current.map((item) => (item.path === path ? { ...item, diff: null, hunks: [] } : item)),
    );
  }

  async function flushPath(path: string) {
    const workspace = settingsRef.current.workspace;
    const doc = tabsRef.current.find((item) => item.path === path);
    if (!workspace || !doc || doc.binary || doc.truncated || doc.draft === doc.content) return;
    const result = await api.saveFile(workspace, path, doc.draft, selectedRef.current ?? undefined);
    if (!result.ok) {
      setError(result.summary);
      return;
    }
    setTabs((current) =>
      current.map((item) =>
        item.path === path ? { ...item, content: item.draft, stale: false, incoming: null } : item,
      ),
    );
  }

  function doHunk(path: string, id: string) {
    const doc = tabsRef.current.find((item) => item.path === path);
    if (!doc) return;
    const nextHunks = doc.hunks.filter((item) => item.id !== id);
    setTabs((current) =>
      current.map((item) =>
        item.path === path ? { ...item, hunks: nextHunks, diff: nextHunks.length ? item.diff : null } : item,
      ),
    );
    if (nextHunks.length === 0) {
      const file = changedRef.current.find((item) => item.path === path);
      rememberAccepted(path, file?.fingerprint ?? fileFingerprint(path, doc.diff));
    }
  }

  async function undoHunk(path: string, id: string, nextContent: string) {
    const doc = tabsRef.current.find((item) => item.path === path);
    if (!doc || !settings.workspace) return;
    if (doc.missing) {
      await undoFile(path);
      return;
    }
    const nextHunks = dropBlock(doc.hunks, id);
    setTabs((current) =>
      current.map((item) =>
        item.path === path
          ? {
              ...item,
              draft: nextContent,
              content: nextContent,
              hunks: nextHunks,
              diff: nextHunks.length ? item.diff : null,
            }
          : item,
      ),
    );
    setSaving(true);
    try {
      const result = await api.saveFile(settings.workspace, path, nextContent, selectedId ?? undefined);
      if (!result.ok) {
        setTabs((current) => current.map((item) => (item.path === path ? doc : item)));
        setError(result.summary);
        return;
      }
      setError("");
      if (nextHunks.length === 0) {
        const file = changedRef.current.find((item) => item.path === path);
        rememberAccepted(path, file?.fingerprint ?? fileFingerprint(path, doc.diff));
      }
    } catch (exc) {
      setTabs((current) => current.map((item) => (item.path === path ? doc : item)));
      setError(exc instanceof Error ? exc.message : "撤销这一块失败");
    } finally {
      setSaving(false);
    }
  }

  async function acceptAgentFile(path: string) {
    await flushPath(path);
    const file = changedRef.current.find((item) => item.path === path);
    rememberAccepted(path, file?.fingerprint ?? fileFingerprint(path, file?.diff ?? null));
  }

  async function acceptAllAgentFiles() {
    const targets = [...pendingChanges];
    for (const file of targets) await flushPath(file.path);
    const next = { ...resolvedRef.current };
    for (const file of targets) next[file.path] = file.fingerprint;
    setResolvedDiffs(next);
    persistResolved(selectedRef.current, next);
    setTabs((current) =>
      current.map((item) =>
        targets.some((file) => file.path === item.path) ? { ...item, diff: null, hunks: [] } : item,
      ),
    );
  }

  async function reloadTab(path: string) {
    if (!settings.workspace) return;
    try {
      const data = await api.file(settings.workspace, path, selectedId ?? undefined);
      setTabs((current) =>
        current.map((item) =>
          item.path === path
            ? {
                ...item,
                content: data.content,
                draft: data.content,
                binary: data.binary,
                truncated: data.truncated,
                diff: null,
                hunks: [],
                stale: false,
                incoming: null,
                missing: false,
              }
            : item,
        ),
      );
    } catch {
      setError(`无法重新加载 ${path}`);
    }
  }

  async function undoFile(path: string) {
    if (!selectedId) return;
    const file = changed.find((item) => item.path === path);
    const result = await api.undo(selectedId, settings.workspace, path);
    setError(result.ok ? "" : result.summary);
    if (result.ok) {
      rememberAccepted(path, file?.fingerprint ?? fileFingerprint(path, file?.diff ?? null));
      await refreshTree();
      if (tabsRef.current.some((item) => item.path === path)) await reloadTab(path);
    }
  }

  async function undoAll() {
    if (!selectedId) return;
    if (!window.confirm("撤销全部未接受的 Agent 修改？")) return;
    const targets = [...pendingChanges];
    for (const file of targets) {
      await api.undo(selectedId, settings.workspace, file.path);
      rememberAccepted(file.path, file.fingerprint);
    }
    await refreshTree();
    for (const file of targets) {
      if (tabsRef.current.some((item) => item.path === file.path)) await reloadTab(file.path);
    }
  }

  function persistLayout(next: LayoutState) {
    layoutRef.current = next;
    setLayout(next);
    saveLayout(next);
  }

  function moveLeft(clientX: number) {
    const box = layoutBox.current?.getBoundingClientRect();
    if (!box) return;
    persistLayout({
      ...layoutRef.current,
      left: clamp(clientX - box.left, 180, Math.min(420, box.width - 520)),
    });
  }

  function moveInspector(clientX: number) {
    const box = layoutBox.current?.getBoundingClientRect();
    if (!box) return;
    const current = layoutRef.current;
    const handle = 6;
    const left = current.sessionHidden ? 0 : current.left;
    const maxInspector = Math.max(260, box.width - left - handle * 2 - 280);
    const width = current.inspectorOnRight
      ? box.right - clientX
      : clientX - box.left - left - handle;
    persistLayout({
      ...current,
      inspector: clamp(width, 260, maxInspector),
    });
  }

  function swapPanes() {
    persistLayout({ ...layoutRef.current, inspectorOnRight: !layoutRef.current.inspectorOnRight });
  }

  function moveTree(clientX: number) {
    const box = filesPane.current?.getBoundingClientRect();
    if (!box) return;
    persistLayout({
      ...layoutRef.current,
      tree: clamp(clientX - box.left, 120, Math.min(360, box.width - 180)),
    });
  }

  function moveBottom(clientY: number) {
    const box = inspectorBox.current?.getBoundingClientRect();
    if (!box) return;
    persistLayout({
      ...layoutRef.current,
      bottom: clamp(box.bottom - clientY, 80, box.height - 120),
    });
  }

  async function createWorkspaceItem(path: string, kind: "file" | "dir") {
    if (!path.trim() || !settings.workspace) return;
    const relative = path.trim().replaceAll("\\", "/");
    const result =
      kind === "dir"
        ? await api.mkdir(settings.workspace, relative)
        : await api.createFile(settings.workspace, relative);
    setError(result.ok ? "" : result.summary);
    if (result.ok) {
      await refreshTree();
      if (kind === "file") await openWorkspaceFile(relative);
    }
  }

  function rewriteOpenPaths(from: string, to: string) {
    setTabs((current) =>
      current.map((item) => {
        if (item.path === from) return { ...item, path: to };
        if (item.path.startsWith(`${from}/`)) {
          return { ...item, path: `${to}${item.path.slice(from.length)}` };
        }
        return item;
      }),
    );
    setActivePath((current) => {
      if (!current) return current;
      if (current === from) return to;
      if (current.startsWith(`${from}/`)) return `${to}${current.slice(from.length)}`;
      return current;
    });
  }

  async function renameWorkspaceItem(from: string, to: string) {
    if (!settings.workspace || from === to) return;
    const result = await api.renamePath(settings.workspace, from, to);
    setError(result.ok ? "" : result.summary);
    if (!result.ok) return;
    rewriteOpenPaths(from, result.path || to);
    await refreshTree();
  }

  async function deleteWorkspaceItem(path: string, kind: "file" | "dir") {
    if (!settings.workspace) return;
    const label = kind === "dir" ? `删除文件夹 ${path} 及其内容？` : `删除文件 ${path}？`;
    if (!window.confirm(label)) return;
    const result = await api.deletePath(settings.workspace, path);
    setError(result.ok ? "" : result.summary);
    if (!result.ok) return;
    const remaining = tabsRef.current.filter(
      (item) => item.path !== path && !item.path.startsWith(`${path}/`),
    );
    setTabs(remaining);
    if (activePath === path || activePath?.startsWith(`${path}/`)) {
      setActivePath(remaining[remaining.length - 1]?.path ?? null);
    }
    await refreshTree();
  }

  async function removeSession(id: string) {
    if (!window.confirm("删除这个会话？聊天记录无法恢复。")) return;
    await api.deleteSession(id);
    if (selectedId === id) {
      setSelectedId(null);
      setEvents([]);
      setClaims([]);
      setTabs([]);
      setActivePath(null);
      setStatus("idle");
    }
    await refreshSessions();
  }

  async function renameSession(id: string, current: string) {
    const title = window.prompt("会话名称", current);
    if (!title?.trim()) return;
    await api.renameSession(id, title.trim());
    await refreshSessions();
  }

  async function exportSession(id: string) {
    try {
      await api.exportSession(id);
      setError("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "导出失败");
    }
  }

  async function importSessionFile(file: File) {
    try {
      const payload = JSON.parse(await file.text()) as unknown;
      const result = await api.importSession(payload);
      await refreshSessions();
      await loadSession(result.session_id);
      setError("");
    } catch (exc) {
      setError(exc instanceof SyntaxError ? "这个文件不是有效的 JSON" : exc instanceof Error ? exc.message : "导入失败");
    }
  }

  const sidebar = (
    <aside className="flex shrink-0 flex-col bg-sidebar" style={{ width: layout.left }}>
      <div className="flex h-10 items-center justify-between gap-1 px-1">
        <div className="flex min-w-0 items-center gap-1">
          <Button
            type="button"
            variant="ghost"
            size="icon"
            title="隐藏会话列表，进入专注模式"
            onClick={() => persistLayout({ ...layoutRef.current, sessionHidden: true })}
          >
            <PanelLeftClose className="h-4 w-4" />
          </Button>
          <span className="truncate text-[12px] font-semibold tracking-[0.16em]">ForgeAgent</span>
        </div>
        <Button variant="ghost" size="icon" onClick={() => setSettingsOpen(true)} title="设置">
          <Settings className="h-4 w-4" />
        </Button>
      </div>
      <div className="flex gap-1 px-2 pb-2">
        <Button className="min-w-0 flex-1 justify-start gap-2" onClick={() => void newChat()}>
          <Plus className="h-3.5 w-3.5" />
          新会话
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          title="导入会话"
          onClick={() => importFileRef.current?.click()}
        >
          导入
        </Button>
        <input
          ref={importFileRef}
          type="file"
          accept="application/json,.json"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void importSessionFile(file);
          }}
        />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {sessions.length === 0 ? (
          <p className="px-2 text-[12px] text-muted-foreground">还没有会话。</p>
        ) : (
          sessions.map((session) => (
            <div
              key={session.id}
              className={cn(
                "group relative mb-0.5 flex items-start rounded-sm transition-colors duration-150 hover:bg-white/[0.04]",
                selectedId === session.id && "bg-white/[0.05]",
              )}
            >
              <span
                className={cn(
                  "absolute inset-y-1.5 left-0 w-0.5 rounded-full bg-primary transition-opacity duration-150",
                  selectedId === session.id ? "opacity-100" : "opacity-0",
                )}
              />
              <button
                type="button"
                onClick={() => void loadSession(session.id)}
                className="min-w-0 flex-1 px-2.5 py-2 text-left"
              >
                <div className="flex items-center gap-2">
                  <div className="min-w-0 flex-1 truncate text-[12px] text-foreground">
                    {session.task || "未命名任务"}
                  </div>
                  {sessionShowsSpinner(session, selectedId, running, status) ? (
                    <Loader2 className="h-3 w-3 shrink-0 animate-spin text-muted-foreground" />
                  ) : (
                    <span
                      className="shrink-0 font-mono text-[11px] text-muted-foreground"
                      title={session.updated}
                    >
                      {timeAgo(session.updated, now)}
                    </span>
                  )}
                </div>
                <div className="mt-0.5 truncate text-[11px] text-muted-foreground">
                  {shortPath(session.workspace_label || session.workspace)}
                </div>
              </button>
              <div className="hidden shrink-0 pr-1 pt-1 group-hover:flex">
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  title="导出会话"
                  className="h-7 px-2 text-[11px]"
                  onClick={() => void exportSession(session.id)}
                >
                  导出
                </Button>
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  title="重命名"
                  onClick={() => void renameSession(session.id, session.task || "")}
                >
                  <Pencil className="h-3 w-3" />
                </Button>
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  title="删除会话"
                  onClick={() => void removeSession(session.id)}
                >
                  <Trash2 className="h-3 w-3" />
                </Button>
              </div>
            </div>
          ))
        )}
      </div>
    </aside>
  );

  const chatPane = (
    <main className="flex min-h-0 min-w-[280px] flex-1 flex-col">
        <header className="flex h-10 items-center justify-between gap-2 border-b border-white/[0.06] px-3">
          <div className="flex min-w-0 items-center gap-1">
            {layout.sessionHidden ? (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                title="显示会话列表"
                onClick={() => persistLayout({ ...layoutRef.current, sessionHidden: false })}
              >
                <PanelLeftOpen className="h-4 w-4" />
              </Button>
            ) : null}
            <div className="flex min-w-0 items-center gap-1">
              <div className="truncate text-[12px] text-muted-foreground">{shortPath(settings.workspace)}</div>
              {settings.workspace && !running ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  title="更换工作区"
                  onClick={() => void changeWorkspace()}
                >
                  <FolderOpen className="h-3.5 w-3.5" />
                </Button>
              ) : null}
            </div>
          </div>
          <div className="flex items-center gap-1">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              title="对调对话区与检查器"
              onClick={swapPanes}
            >
              <ArrowLeftRight className="h-3.5 w-3.5" />
            </Button>
            <div
              className={cn(
                "rounded-full px-2 py-0.5 text-[11px] tracking-wide text-muted-foreground",
                running && "bg-primary/10 text-primary",
                status === "completed" && "text-emerald-400",
                (status === "failed" || status === "cancelled" || status === "stopped") && "text-red-400",
              )}
            >
              {running && liveHint ? liveHint : (STATUS_LABEL[status] ?? status)}
            </div>
          </div>
        </header>

        {pendingChanges.length > 0 ? (
          <div className="border-b border-white/[0.06] bg-primary/[0.04]">
            <div className="flex items-center gap-2 px-3 py-1.5">
              <button
                type="button"
                className="flex items-center gap-2 text-[12.5px] text-foreground"
                onClick={() => setFilesOpen((value) => !value)}
              >
                <ChevronDown className={cn("h-3.5 w-3.5", !filesOpen && "-rotate-90")} />
                已更改 {pendingChanges.length} 个文件
                <LineDelta added={addedTotal} deleted={deletedTotal} />
              </button>
              <div className="ml-auto flex gap-1">
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  title="这些改动已经写进文件。接受只收起对比，不会再写一遍。"
                  onClick={() => void acceptAllAgentFiles()}
                >
                  <Check className="h-3.5 w-3.5" />
                  全部接受
                </Button>
                <Button type="button" size="sm" variant="ghost" title="还原这些文件到 Agent 修改前" onClick={() => void undoAll()}>
                  <Undo2 className="h-3.5 w-3.5" />
                  全部撤销
                </Button>
              </div>
            </div>
            {filesOpen ? (
              <div className="space-y-0.5 px-3 pb-2">
                {pendingChanges.map((file) => (
                  <div key={file.path} className="flex items-center gap-2 rounded px-1 py-0.5 hover:bg-white/5">
                    <button
                      type="button"
                      className="min-w-0 flex-1 truncate text-left text-[12.5px] text-foreground/90"
                      onClick={() => void openWorkspaceFile(file.path)}
                    >
                      {file.path}
                    </button>
                    <ChangeKindLabel kind={file.kind} />
                    <LineDelta added={file.added} deleted={file.deleted} />
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      title="这些改动已经写进文件。接受只收起对比。"
                      onClick={() => void acceptAgentFile(file.path)}
                    >
                      <Check className="h-3 w-3" />
                      接受
                    </Button>
                    <Button type="button" size="sm" variant="ghost" title="撤销 Agent 修改" onClick={() => void undoFile(file.path)}>
                      <Undo2 className="h-3 w-3" />
                      撤销
                    </Button>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}

        <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto flex min-h-full max-w-[720px] flex-col px-4 py-6">
            {blocks.length === 0 ? (
              <div className="m-auto max-w-md text-center">
                <h1 className="text-[22px] font-medium tracking-[0.14em]">准备开始</h1>
                <p className="mt-3 text-[13px] leading-6 text-muted-foreground">
                  先点「新会话」选择本地工作区，再输入编程任务。密钥只存在于启动 GUI 的终端环境变量中。
                </p>
                {!settings.workspace ? (
                  <p className="mt-3 text-[13px] text-amber-400/90">尚未选择工作区。</p>
                ) : null}
                {!hasKey ? (
                  <p className="mt-3 text-[13px] text-amber-400/90">当前进程未检测到 FORGE_API_KEY。</p>
                ) : null}
              </div>
            ) : (
              <div className="space-y-5 pb-4">
                {blocks.map((block, index) => {
                  if (block.type === "user") {
                    return (
                      <div key={`u-${index}`} className="flex justify-end">
                        <div className="max-w-[78%] rounded-md bg-white/[0.08] px-3 py-2 text-[15px] leading-7">
                          {block.text}
                        </div>
                      </div>
                    );
                  }
                  if (block.type === "assistant") {
                    return (
                      <div key={`a-${index}`} className="flex justify-start">
                        <div className="max-w-[92%]">
                          <div className="prose-forge">
                            <Markdown remarkPlugins={[remarkGfm]}>{block.text}</Markdown>
                            {block.streaming ? (
                              <span className="ml-0.5 inline-block h-3 w-0.5 animate-pulse bg-primary/80" />
                            ) : null}
                          </div>
                        </div>
                      </div>
                    );
                  }
                  return (
                    <ProcessGroup
                      key={`p-${index}`}
                      items={block.items}
                      openDefault={running && index === blocks.length - 1}
                    />
                  );
                })}
              </div>
            )}
          </div>
        </div>

        {approval ? (
          <div className="px-4">
          <div className="mx-auto mb-2 flex w-full max-w-[720px] items-start gap-3 rounded-md bg-amber-500/[0.08] px-3 py-2">
            <ShieldAlert className="mt-0.5 h-4 w-4 text-amber-400" />
            <div className="min-w-0 flex-1">
              <div className="text-[13px]">
                {approval.kind === "plan"
                  ? "是否按此方案执行？"
                  : FILE_WRITE_TOOLS.has(approval.tool)
                    ? "允许本次任务写入工作区文件？"
                    : `需要审批：${approval.tool}`}
              </div>
              <div className="text-[12px] text-muted-foreground">
                {approval.kind === "plan"
                  ? "方案已在上方对话中。点「执行」才改代码，点「先不改」则停在方案。"
                  : FILE_WRITE_TOOLS.has(approval.tool)
                    ? "允许后，本轮任务里再写文件、改文件或删文件都不再询问。运行命令仍会逐次确认。"
                    : approval.reason}
              </div>
              {approval.kind === "plan" ? null : Object.keys(approval.arguments).length > 0 ? (
                <pre className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap break-all text-[11px] text-muted-foreground">
                  {JSON.stringify(approval.arguments, null, 2)}
                </pre>
              ) : null}
              <div className="mt-2 flex flex-wrap gap-2">
                {approval.kind === "plan" ? (
                  <Button
                    size="sm"
                    onClick={() => void api.approve(approval.id, { approved: true, scope: "once" })}
                  >
                    执行
                  </Button>
                ) : FILE_WRITE_TOOLS.has(approval.tool) ? (
                  <>
                    <Button
                      size="sm"
                      onClick={() => void api.approve(approval.id, { approved: true, scope: "run" })}
                    >
                      允许本次任务写文件
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => void api.approve(approval.id, { approved: true, scope: "once" })}
                    >
                      只允许这一次
                    </Button>
                  </>
                ) : (
                  <>
                    <Button
                      size="sm"
                      onClick={() => void api.approve(approval.id, { approved: true, scope: "once" })}
                    >
                      允许
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => void api.approve(approval.id, { approved: true, scope: "session" })}
                    >
                      本会话记住此类命令
                    </Button>
                  </>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void api.approve(approval.id, { approved: false, scope: "once" })}
                >
                  {approval.kind === "plan" ? "先不改" : "拒绝"}
                </Button>
              </div>
            </div>
          </div>
          </div>
        ) : null}

        {error ? <div className="mx-auto mb-2 w-full max-w-[720px] px-4 text-[12.5px] text-red-400">{error}</div> : null}
        {running && liveHint ? (
          <div className="mx-auto mb-2 w-full max-w-[720px] px-4 text-[12.5px] text-muted-foreground">{liveHint}</div>
        ) : null}

        <form onSubmit={(event) => void submit(event)} className="border-t border-white/[0.06] px-4 pb-3 pt-2">
          <input
            ref={attachInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={(event) => {
              const files = Array.from(event.target.files ?? []);
              event.target.value = "";
              if (files.length) void ingestFiles(files);
            }}
          />
          <div
            className={cn(
              "mx-auto max-w-[720px] rounded-md bg-white/[0.03] px-1.5 py-1 transition-shadow duration-150",
              dropActive && "bg-primary/[0.05] ring-1 ring-primary/50",
            )}
            onDragEnter={onComposerDragEnter}
            onDragOver={onComposerDragOver}
            onDragLeave={onComposerDragLeave}
            onDrop={(event) => void onComposerDrop(event)}
          >
            {chatRefs.length > 0 ? (
              <div className="flex flex-wrap gap-1 px-2 pt-1">
                {chatRefs.map((ref) => (
                  <span
                    key={ref.id}
                    className="flex max-w-full items-center gap-1 rounded-md bg-white/[0.06] px-2 py-0.5 text-[11px]"
                  >
                    <span className="flex items-center gap-1 truncate">
                      {ref.kind === "image" && ref.preview ? (
                        <img src={ref.preview} alt="" className="h-5 w-5 rounded object-cover" />
                      ) : null}
                      {ref.kind === "image"
                        ? ref.label || "截图"
                        : ref.startLine
                          ? `${ref.path}:${ref.startLine}${ref.endLine && ref.endLine !== ref.startLine ? `-${ref.endLine}` : ""}`
                          : ref.label || ref.path}
                    </span>
                    <button
                      type="button"
                      className="text-muted-foreground hover:text-foreground"
                      onClick={() => setChatRefs((current) => current.filter((item) => item.id !== ref.id))}
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
              </div>
            ) : null}
            <textarea
              value={task}
              onChange={(event) => setTask(event.target.value)}
              onKeyDown={onComposerKey}
              onPaste={(event) => void onComposerPaste(event)}
              rows={1}
              placeholder={
                selectedId
                  ? "继续此会话… 可拖入文件，@ 插工作区文件，Ctrl+V 可贴图，Shift+Enter 换行"
                  : "输入编程任务，可拖入文件，@ 插工作区文件，Ctrl+V 可贴图，Enter 发送"
              }
              className="max-h-40 min-h-[44px] w-full resize-none bg-transparent px-2 py-2 text-[14px] outline-none placeholder:text-muted-foreground"
            />
            <div className="flex items-center justify-between px-1 pb-0.5">
              <div className="flex items-center gap-2">
                <ModeSelect
                  value={settings.mode}
                  onChange={(mode) => {
                    const next = { ...settings, mode };
                    setSettings(next);
                    saveSettings(next);
                    if (selectedId) void api.saveSettings(selectedId, { mode });
                  }}
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  title="插入工作区文件到提问"
                  onClick={() => {
                    setPaletteMode("insert");
                    setPaletteOpen(true);
                    setPaletteQuery("");
                  }}
                >
                  <AtSign className="h-3.5 w-3.5" />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  title="附上工作区外的文件"
                  onClick={() => attachInputRef.current?.click()}
                >
                  <Paperclip className="h-3.5 w-3.5" />
                </Button>
                {openFile ? (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    title="把当前打开的文件插入提问"
                    onClick={() => insertFileRef(openFile.path)}
                  >
                    当前文件
                  </Button>
                ) : null}
                {selectedId && !running ? (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      if (!window.confirm("撤销本轮全部 Agent 修改（所有相关文件）？")) return;
                      void api
                        .rollback(selectedId, settings.workspace)
                        .then((result) => setError(result.ok ? "" : result.summary));
                    }}
                  >
                    <RotateCcw className="h-3.5 w-3.5" />
                    撤销本轮全部修改
                  </Button>
                ) : null}
              </div>
              {running ? (
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  onClick={() => selectedId && void api.cancel(selectedId)}
                >
                  <Square className="h-3 w-3 fill-current" />
                  停止
                </Button>
              ) : (
                <div className="flex items-center gap-1">
                  {canRetry ? (
                    <Button type="button" variant="secondary" size="sm" onClick={() => void retryLast()}>
                      接着试
                    </Button>
                  ) : null}
                  <Button type="submit" size="sm" disabled={!task.trim() && chatRefs.length === 0}>
                    <SendHorizontal className="h-3.5 w-3.5" />
                    发送
                  </Button>
                </div>
              )}
            </div>
          </div>
        </form>
    </main>
  );

  const inspectorPane = (
      <aside
        ref={inspectorBox}
        className="flex min-h-0 shrink-0 flex-col overflow-hidden bg-inspector"
        style={{ width: layout.inspector }}
      >
        <InspectorWorkspace
          ref={inspector}
          bottomHeight={layout.bottom}
          onBottomResize={moveBottom}
          onSplit={(half) => persistLayout({ ...layoutRef.current, bottom: half })}
          render={(page: InspectorPage) => {
            if (page.kind === "files") return (
            <div ref={filesPane} className="flex h-full min-h-0 min-w-0 flex-1">
              <div className="flex min-w-[120px] shrink-0 flex-col" style={{ width: layout.tree }}>
                <FileTree
                  nodes={tree}
                  active={openFile?.path ?? null}
                  changed={changedPaths}
                  onOpen={(path) => void openWorkspaceFile(path)}
                  onCreate={(path, kind) => void createWorkspaceItem(path, kind)}
                  onRename={(from, to) => void renameWorkspaceItem(from, to)}
                  onDelete={(path, kind) => void deleteWorkspaceItem(path, kind)}
                />
              </div>
              <SplitHandle onMove={moveTree} />
              <div className="flex min-h-0 min-w-0 flex-1 flex-col">
                {tabs.length > 0 ? (
                  <div className="flex shrink-0 overflow-x-auto border-b border-white/[0.06]">
                    {tabs.map((doc) => (
                      <div
                        key={doc.path}
                        className={cn(
                          "flex max-w-[240px] items-center border-b-2 -mb-px transition-colors duration-150",
                          activePath === doc.path
                            ? "border-foreground/70 text-foreground"
                            : "border-transparent text-muted-foreground hover:text-foreground",
                        )}
                      >
                        <button
                          type="button"
                          className="min-w-0 truncate px-2 py-1 text-left text-[11px]"
                          onClick={() => setActivePath(doc.path)}
                          title={doc.path}
                        >
                          {doc.path.split("/").pop()}
                          {doc.draft !== doc.content ? <span className="ml-1 text-amber-400">●</span> : null}
                        </button>
                        <button
                          type="button"
                          className="px-1 text-muted-foreground hover:text-foreground"
                          onClick={() => void closeTab(doc.path)}
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </div>
                    ))}
                  </div>
                ) : null}
                {openFile ? (
                  <>
                    {openFile.stale ? (
                      <div className="flex items-center gap-2 border-b border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[12px]">
                        <span className="min-w-0 flex-1">磁盘上有新内容（Agent 刚写过这个文件）。</span>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => {
                            if (!openFile.incoming) return;
                            const incoming = openFile.incoming;
                            setTabs((current) =>
                              current.map((item) =>
                                item.path === openFile.path
                                  ? { ...item, content: incoming, draft: incoming, stale: false, incoming: null }
                                  : item,
                              ),
                            );
                          }}
                        >
                          加载磁盘版本
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() =>
                            setTabs((current) =>
                              current.map((item) =>
                                item.path === openFile.path ? { ...item, stale: false } : item,
                              ),
                            )
                          }
                        >
                          保留我的编辑
                        </Button>
                      </div>
                    ) : null}
                    <div className="flex items-center gap-2 border-b border-white/[0.06] px-2 py-1 text-[12px]">
                      <span className="min-w-0 flex-1 truncate">
                        {openFile.path}
                        {dirty ? <span className="ml-1 text-amber-400">●</span> : null}
                      </span>
                      {openFile.hunks.length > 0 ? <LineDelta {...hunkStats(openFile.hunks)} /> : null}
                      {openFile.hunks.length > 0 && selectedId ? (
                        <Button
                          size="sm"
                          variant="ghost"
                          title="撤销 Agent 对本文件的修改"
                          onClick={() => void undoFile(openFile.path)}
                        >
                          <Undo2 className="h-3 w-3" />
                          撤销
                        </Button>
                      ) : null}
                      <Button
                        size="sm"
                        variant={dirty ? "default" : "ghost"}
                        disabled={saving || openFile.binary || openFile.truncated || openFile.missing || (!dirty && !openFile.stale)}
                        title="把你在编辑器里手改的内容写回磁盘"
                        onClick={() => void saveOpenFile(true)}
                      >
                        <Save className="h-3 w-3" />
                        {saving ? "保存中" : "保存"}
                      </Button>
                    </div>
                    {openFile.missing ? (
                      <p className="border-b border-white/[0.06] px-3 py-2 text-[12px] text-red-300/90">
                        文件已被 Agent 删除。接受将保持删除；撤销可恢复到删除前。
                      </p>
                    ) : null}
                    {openFile.image && openFile.content.startsWith("data:") ? (
                      <div className="min-h-0 flex-1 overflow-auto p-3">
                        <img src={openFile.content} alt={openFile.path} className="max-w-full rounded-md" />
                      </div>
                    ) : openFile.binary ? (
                      <p className="p-3 text-[12px] text-muted-foreground">二进制文件无法在此编辑。</p>
                    ) : openFile.truncated ? (
                      <div className="min-h-0 flex-1 overflow-auto p-3">
                        <p className="mb-2 text-[12px] text-muted-foreground">文件过大，只显示开头，不能在此保存。</p>
                        <pre className="whitespace-pre-wrap break-words font-mono text-[12px] leading-5">{openFile.content}</pre>
                      </div>
                    ) : (
                      <CodeEditor
                        path={openFile.path}
                        value={draft}
                        onChange={setDraft}
                        hunks={openFile.hunks}
                        onSave={() => void saveOpenFile(true)}
                        onHunkDo={(id) => doHunk(openFile.path, id)}
                        onHunkUndo={(id, next) => void undoHunk(openFile.path, id, next)}
                        onAskSelection={askSelection}
                      />
                    )}
                  </>
                ) : (
                  <p className="p-3 text-[12px] text-muted-foreground">
                    点击左侧文件即可编辑。Ctrl+P 搜索文件，Ctrl+S 保存，Ctrl+F 查找，Ctrl+H 替换。可在当前目录右键新建文件。选中代码后可向 Agent 提问。对话可拖入外部文件或粘贴截图。
                  </p>
                )}
              </div>
            </div>
            );
            if (page.kind === "git") {
              return <GitPanel workspace={settings.workspace} onChanged={() => void refreshAfterGit()} />;
            }
            if (page.kind === "evidence") return <EvidencePanel claims={claims} />;
            if (page.kind === "context") return (
            <div className="h-full overflow-y-auto px-3 py-2 text-[12.5px]">
              {context ? (
                <div className="space-y-2">
                  <p className="text-muted-foreground">当前提示词大约占用多少 Token，供你判断上下文是否过长。</p>
                  {typeof context.total_tokens === "number" || typeof context.input_limit === "number" ? (
                    <p>
                      已用 {String(context.total_tokens ?? "—")} / 上限 {String(context.input_limit ?? "—")}
                    </p>
                  ) : null}
                  {typeof context.before === "number" ? (
                    <p>
                      压缩前 {String(context.before)} → 压缩后 {String(context.after)}，整理了{" "}
                      {String(context.messages ?? "—")} 条消息
                    </p>
                  ) : null}
                  <div className="space-y-1 text-[12px] text-muted-foreground">
                    {[
                      ["system_tokens", "系统说明"],
                      ["project_tokens", "项目说明"],
                      ["summary_tokens", "压缩摘要"],
                      ["recent_tokens", "最近对话"],
                      ["tool_schema_tokens", "工具说明"],
                      ["truncated_tool_outputs", "被截断的工具输出"],
                    ].map(([key, label]) =>
                      context[key] == null ? null : (
                        <div key={key} className="flex justify-between gap-2">
                          <span>{label}</span>
                          <span>{String(context[key])}</span>
                        </div>
                      ),
                    )}
                  </div>
                </div>
              ) : (
                <p className="text-muted-foreground">Agent 开始组装提示词后，这里会显示 Token 占用。</p>
              )}
            </div>
            );
            return (
              <div className="h-full min-h-0 min-w-0 flex-1 bg-background">
                <WorkspaceTerminal workspace={settings.workspace} sessionId={page.id} />
              </div>
            );
          }}
        />
      </aside>
  );

  return (
    <div ref={layoutBox} className="flex h-full bg-background">
      {layout.sessionHidden ? null : (
        <>
          {sidebar}
          <SplitHandle onMove={moveLeft} />
        </>
      )}
      {layout.inspectorOnRight ? (
        <>
          {chatPane}
          <SplitHandle onMove={moveInspector} onSwap={swapPanes} />
          {inspectorPane}
        </>
      ) : (
        <>
          {inspectorPane}
          <SplitHandle onMove={moveInspector} onSwap={swapPanes} />
          {chatPane}
        </>
      )}

      {paletteOpen ? (
        <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/55 pt-24" onClick={() => setPaletteOpen(false)}>
          <div
            className="w-[480px] rounded-lg bg-popover p-2 ring-1 ring-white/[0.08]"
            onClick={(event) => event.stopPropagation()}
          >
            <input
              autoFocus
              value={paletteQuery}
              onChange={(event) => setPaletteQuery(event.target.value)}
              placeholder={paletteMode === "insert" ? "选择文件插入提问" : "搜索工作区文件"}
              className="h-9 w-full bg-transparent px-3 text-[13px] outline-none"
              onKeyDown={(event) => {
                if (event.key === "Escape") setPaletteOpen(false);
                if (event.key === "Enter" && paletteFiles[0]) {
                  if (paletteMode === "insert") insertFileRef(paletteFiles[0]);
                  else void openWorkspaceFile(paletteFiles[0]);
                  setPaletteOpen(false);
                }
              }}
            />
            <div className="mt-1 max-h-72 overflow-y-auto">
              {paletteFiles.slice(0, 40).map((path) => (
                <button
                  key={path}
                  type="button"
                  className="block w-full truncate rounded px-2 py-1.5 text-left text-[12.5px] hover:bg-white/5"
                  onClick={() => {
                    if (paletteMode === "insert") insertFileRef(path);
                    else void openWorkspaceFile(path);
                    setPaletteOpen(false);
                  }}
                >
                  {path}
                </button>
              ))}
            </div>
          </div>
        </div>
      ) : null}

      {settingsOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/55" onClick={() => setSettingsOpen(false)}>
          <div
            className="w-[480px] rounded-lg bg-popover p-4 ring-1 ring-white/[0.08]"
            onClick={(event) => event.stopPropagation()}
          >
            <h2 className="mb-3 text-sm font-semibold">{selectedId ? "当前会话设置" : "新会话设置"}</h2>
            <label className="mb-2 block text-[12px] text-muted-foreground">
              工作区
              <div className="mt-1 flex gap-2">
                <input
                  value={settings.workspace}
                  readOnly
                  className="h-8 min-w-0 flex-1 rounded-md bg-white/[0.04] px-2 text-[13px]"
                />
                <Button
                  type="button"
                  variant="outline"
                  disabled={running}
                  onClick={() => void changeWorkspace()}
                >
                  <FolderOpen className="h-3.5 w-3.5" />
                  {selectedId ? "更换" : "选择"}
                </Button>
              </div>
            </label>
            <label className="mb-2 block text-[12px] text-muted-foreground">
              模型
              <input
                value={settings.model}
                onChange={(event) => setSettings({ ...settings, model: event.target.value })}
                className="mt-1 h-8 w-full rounded-md bg-white/[0.04] px-2 text-[13px]"
              />
            </label>
            <label className="mb-2 block text-[12px] text-muted-foreground">
              验证命令
              <input
                value={settings.verify}
                onChange={(event) => setSettings({ ...settings, verify: event.target.value })}
                className="mt-1 h-8 w-full rounded-md bg-white/[0.04] px-2 text-[13px]"
              />
            </label>
            <div className="mb-2 grid grid-cols-3 gap-2">
              <label className="text-[12px] text-muted-foreground">
                最大步数
                <input
                  type="number"
                  value={settings.max_steps}
                  onChange={(event) => setSettings({ ...settings, max_steps: Number(event.target.value) })}
                  className="mt-1 h-8 w-full rounded-md bg-white/[0.04] px-2 text-[13px]"
                />
              </label>
              <label className="text-[12px] text-muted-foreground">
                Token 上限
                <input
                  type="number"
                  value={settings.max_tokens}
                  onChange={(event) => setSettings({ ...settings, max_tokens: Number(event.target.value) })}
                  className="mt-1 h-8 w-full rounded-md bg-white/[0.04] px-2 text-[13px]"
                />
              </label>
              <label className="text-[12px] text-muted-foreground">
                费用上限
                <input
                  value={settings.max_cost}
                  onChange={(event) => setSettings({ ...settings, max_cost: event.target.value })}
                  className="mt-1 h-8 w-full rounded-md bg-white/[0.04] px-2 text-[13px]"
                />
              </label>
            </div>
            <label className="mb-2 flex items-center gap-2 text-[13px]">
              <input
                type="checkbox"
                checked={settings.auto_approve}
                onChange={(event) => setSettings({ ...settings, auto_approve: event.target.checked })}
              />
              自动批准中风险操作
            </label>
            <label className="mb-4 flex items-center gap-2 text-[13px]">
              <input
                type="checkbox"
                checked={settings.demo}
                onChange={(event) => setSettings({ ...settings, demo: event.target.checked })}
              />
              截图时隐藏本机路径
            </label>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setSettingsOpen(false)}>
                取消
              </Button>
              <Button
                onClick={() => {
                  saveSettings(settings);
                  if (selectedId) void api.saveSettings(selectedId, settings);
                  setSettingsOpen(false);
                }}
              >
                保存
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
