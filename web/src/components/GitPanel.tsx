import { useEffect, useRef, useState } from "react";
import { ChevronDown, GitBranch, Loader2, RotateCcw, Save, Upload } from "lucide-react";
import { api, type GitSnapshot } from "../lib/api";
import { Button } from "./ui/button";
import { cn, timeAgo } from "../lib/utils";

const KIND_LABEL: Record<string, string> = {
  modified: "已修改",
  added: "新文件",
  deleted: "已删除",
  untracked: "未跟踪",
};

export function GitPanel({
  workspace,
  onChanged,
}: {
  workspace: string;
  onChanged: () => void;
}) {
  const [state, setState] = useState<GitSnapshot | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pushing, setPushing] = useState(false);
  const [message, setMessage] = useState("");
  const [branchName, setBranchName] = useState("");
  const [remoteUrl, setRemoteUrl] = useState("");
  const [notice, setNotice] = useState("");
  const [branchOpen, setBranchOpen] = useState(false);
  const branchMenuRef = useRef<HTMLDivElement>(null);

  async function load() {
    if (!workspace) {
      setState(null);
      return;
    }
    try {
      setState(await api.gitStatus(workspace));
      setError("");
      setNotice("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "无法读取 Git 状态");
    }
  }

  useEffect(() => {
    void load();
  }, [workspace]);

  useEffect(() => {
    if (state?.remote_url) setRemoteUrl(state.remote_url);
  }, [state?.remote_url]);

  useEffect(() => {
    if (!branchOpen) return;
    function onPointerDown(event: MouseEvent) {
      if (branchMenuRef.current && !branchMenuRef.current.contains(event.target as Node)) {
        setBranchOpen(false);
      }
    }
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [branchOpen]);

  async function run(action: () => Promise<GitSnapshot>) {
    setBusy(true);
    setError("");
    try {
      const next = await action();
      setState(next);
      onChanged();
      return true;
    } catch (exc) {
      setNotice("");
      setError(exc instanceof Error ? exc.message : "操作失败");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function pushCurrent() {
    setPushing(true);
    setError("");
    setNotice("");
    try {
      const next = await api.gitPush(workspace);
      setState(next);
      onChanged();
      setNotice(`已把当前分支 ${next.branch} 推送到 GitHub`);
    } catch (exc) {
      setNotice("");
      setError(exc instanceof Error ? exc.message : "推送失败");
    } finally {
      setPushing(false);
    }
  }

  if (!workspace) {
    return <p className="p-3 text-[12px] text-muted-foreground">先选择工作区，才能给这个项目做版本存档。</p>;
  }

  if (!state) {
    return <p className="p-3 text-[12px] text-muted-foreground">{error || "正在读取 Git 状态…"}</p>;
  }

  if (!state.available) {
    return (
      <div className="space-y-2 p-3 text-[12px]">
        <p className="text-muted-foreground">{state.reason || "未找到 Git。"}</p>
      </div>
    );
  }

  if (!state.repo) {
    return (
      <div className="space-y-3 p-3 text-[12.5px]">
        <p className="text-muted-foreground">
          这个文件夹还没有版本存档。开始后，你可以先保存一个能跑的版本，再让智能体改；改坏了就能恢复回来。
        </p>
        {error ? <p className="text-red-300">{error}</p> : null}
        <Button size="sm" disabled={busy} onClick={() => void run(() => api.gitInit(workspace))}>
          开始版本控制
        </Button>
      </div>
    );
  }

  const branchNodes = state.branch_nodes.length
    ? state.branch_nodes
    : state.branches.map((name) => ({
        name,
        parent: "",
        depth: 0,
        current: name === state.branch,
        same_version: false,
        ahead: 0,
        label: "",
      }));

  return (
    <div className="flex h-full min-h-0 flex-col overflow-y-auto px-3 py-3 text-[12.5px]">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="text-[13px] font-medium">版本控制</h2>
        <div className="relative" ref={branchMenuRef}>
          <button
            type="button"
            disabled={busy || branchNodes.length === 0}
            onClick={() => setBranchOpen((value) => !value)}
            className="flex h-7 max-w-[160px] items-center gap-1 rounded-md border border-border px-2 text-[12px] hover:bg-white/5 disabled:opacity-50"
          >
            <GitBranch className="h-3 w-3 shrink-0 text-muted-foreground" />
            <span className="min-w-0 flex-1 truncate text-left">{state.branch || "选择分支"}</span>
            <ChevronDown className={cn("h-3 w-3 shrink-0 text-muted-foreground", branchOpen && "rotate-180")} />
          </button>
          {branchOpen ? (
            <div className="absolute right-0 z-30 mt-1 min-w-[200px] overflow-hidden rounded-md border border-border bg-[#1a1a1a] py-1 shadow-xl">
              {branchNodes.map((node) => (
                <button
                  key={node.name}
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setBranchOpen(false);
                    if (node.name !== state.branch) void run(() => api.gitCheckout(workspace, node.name));
                  }}
                  className={cn(
                    "flex w-full items-center gap-1 py-0.5 pr-3 text-left hover:bg-white/5",
                    node.current && "bg-primary/15 text-primary",
                  )}
                  style={{ paddingLeft: 8 + node.depth * 14 }}
                >
                  <GitBranch className="h-3 w-3 shrink-0 opacity-60" />
                  <span className="truncate">{node.name}</span>
                </button>
              ))}
            </div>
          ) : null}
        </div>
      </div>

      <p className="mb-2 text-[12px] text-muted-foreground">
        当前分支：<span className="font-medium text-foreground">{state.branch || "无"}</span>
      </p>

      <label className="mb-1 text-[12px] text-muted-foreground">GitHub 仓库地址</label>
      <div className="mb-2 flex gap-2">
        <input
          value={remoteUrl}
          onChange={(event) => setRemoteUrl(event.target.value)}
          placeholder="https://github.com/用户名/仓库名.git"
          className="h-7 min-w-0 flex-1 rounded-md border border-border bg-transparent px-2 text-[12px] outline-none"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={busy || !remoteUrl.trim()}
          onClick={() => {
            setNotice("");
            void run(() => api.gitRemote(workspace, remoteUrl.trim()));
          }}
        >
          保存地址
        </Button>
      </div>
      <div className="mb-3 flex items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={pushing || !state.branch}
          onClick={() => void pushCurrent()}
        >
          <Upload className="h-3 w-3" />
          推送当前分支
        </Button>
        {pushing ? <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-muted-foreground" /> : null}
        {pushing ? <span className="text-[12px] text-muted-foreground">正在推送到 GitHub…</span> : null}
      </div>

      <div className="mb-3 flex gap-2">
        <input
          value={branchName}
          onChange={(event) => setBranchName(event.target.value)}
          placeholder={state.branch ? `从 ${state.branch} 再分一条，例如 2` : "新分支名"}
          className="h-7 min-w-0 flex-1 rounded-md border border-border bg-transparent px-2 text-[12px] outline-none"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={busy || !branchName.trim()}
          onClick={() => {
            const name = branchName.trim();
            void (async () => {
              if (await run(() => api.gitBranch(workspace, name))) setBranchName("");
            })();
          }}
        >
          开新分支
        </Button>
      </div>

      <label className="mb-1 text-[12px] text-muted-foreground">保存说明</label>
      <textarea
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        rows={3}
        placeholder="例如：登录功能可以跑了，准备让智能体改样式"
        className="mb-2 w-full resize-none rounded-md border border-border bg-transparent px-2 py-1.5 text-[12px] outline-none"
      />
      <Button
        size="sm"
        disabled={busy || !message.trim()}
        onClick={() => {
          const text = message.trim();
          void (async () => {
            if (await run(() => api.gitCommit(workspace, text))) setMessage("");
          })();
        }}
      >
        <Save className="h-3 w-3" />
        保存这个版本
      </Button>

      {error ? <p className="mt-2 text-red-300">{error}</p> : null}
      {notice ? <p className="mt-2 text-emerald-300">{notice}</p> : null}

      <div className="mt-4">
        <h3 className="mb-1 text-[12px] font-medium text-muted-foreground">当前未保存改动</h3>
        {state.dirty.length === 0 ? (
          <p className="text-[12px] text-muted-foreground">工作区是干净的，和最新保存的版本一致。</p>
        ) : (
          <ul className="space-y-0.5">
            {state.dirty.map((item) => (
              <li key={item.path} className="truncate text-[12px]">
                <span className="mr-2 text-muted-foreground">{KIND_LABEL[item.kind] ?? item.kind}</span>
                {item.path}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="mt-4 min-h-0 flex-1">
        <h3 className="mb-1 text-[12px] font-medium text-muted-foreground">已保存的版本</h3>
        {state.commits.length === 0 ? (
          <p className="text-[12px] text-muted-foreground">还没有版本。写好说明后点「保存这个版本」。</p>
        ) : (
          <ul className="space-y-2">
            {state.commits.map((commit) => (
              <li key={commit.sha} className="rounded-md border border-white/[0.06] bg-white/[0.02] px-2 py-2">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-medium">{commit.message}</p>
                    <p className="text-[11px] text-muted-foreground">
                      {commit.short} · {commit.author} · {timeAgo(commit.when) || commit.when}
                    </p>
                  </div>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy}
                    title="用这个版本覆盖当前工作区"
                    onClick={() => {
                      if (
                        !window.confirm(
                          `恢复到「${commit.message}」？当前未保存改动和这之后的提交会从当前分支上拿掉，智能体多出来的未跟踪文件也会删掉。`,
                        )
                      ) {
                        return;
                      }
                      void run(() => api.gitRestore(workspace, commit.sha, true));
                    }}
                  >
                    <RotateCcw className="h-3 w-3" />
                    恢复
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
