import { useEffect, useState } from "react";
import { Bookmark } from "lucide-react";
import { api, type MemoryItem, type MemoryStore } from "../lib/api";
import { Button } from "./ui/button";
import { cn } from "../lib/utils";

const KIND_LABEL: Record<string, string> = {
  preference: "个人偏好",
  convention: "开发规范",
  pitfall: "踩过的坑",
  fact: "项目事实",
};

export function MemoryPanel({
  workspace,
  revision,
}: {
  workspace: string;
  revision: number;
}) {
  const [store, setStore] = useState<MemoryStore | null>(null);
  const [error, setError] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  async function reload() {
    if (!workspace) {
      setStore(null);
      return;
    }
    try {
      const data = await api.memory(workspace);
      setStore(data);
      setError("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "无法读取记忆");
    }
  }

  useEffect(() => {
    void reload();
  }, [workspace, revision]);

  async function setAutoExtract(enabled: boolean) {
    if (!workspace) return;
    const data = await api.patchMemory(workspace, { auto_extract: enabled });
    setStore(data);
  }

  async function acceptAll() {
    if (!workspace) return;
    const data = await api.patchMemory(workspace, { accept_all: true });
    setStore(data);
  }

  async function acceptOne(id: string) {
    if (!workspace) return;
    await api.updateMemory(workspace, id, { status: "accepted" });
    await reload();
  }

  async function saveText(id: string) {
    if (!workspace) return;
    const text = drafts[id];
    if (text == null) return;
    await api.updateMemory(workspace, id, { text });
    setDrafts((current) => {
      const next = { ...current };
      delete next[id];
      return next;
    });
    await reload();
  }

  async function remove(id: string) {
    if (!workspace) return;
    await api.deleteMemory(workspace, id);
    await reload();
  }

  if (!workspace) {
    return <p className="p-3 text-[12px] text-muted-foreground">先选择工作区后，这里会列出跨会话记忆。</p>;
  }
  if (error) {
    return <p className="p-3 text-[12px] text-red-400">{error}</p>;
  }
  if (!store) {
    return <p className="p-3 text-[12px] text-muted-foreground">正在读取记忆…</p>;
  }

  const proposed = store.items.filter((item) => item.status === "proposed");
  const accepted = store.items.filter((item) => item.status === "accepted");

  return (
    <div className="h-full overflow-y-auto px-3 py-3">
      <div className="mb-3 space-y-1">
        <h2 className="text-[13px] font-medium">跨会话记忆</h2>
        <p className="text-[12px] leading-5 text-muted-foreground">
          每轮正常结束后，会抽出个人偏好、开发规范和踩过的坑，写入工作区 .forge/memory.jsonl，不进更改横栏。不记录仓库里有哪些文件或默认算法。下次新会话只注入相关条目。待确认的规范/踩坑会先带上，偏好需你点接受。
        </p>
      </div>
      <label className="mb-3 flex items-center gap-2 text-[12.5px]">
        <input
          type="checkbox"
          checked={store.auto_extract}
          onChange={(event) => void setAutoExtract(event.target.checked)}
        />
        对话结束后自动抽取
      </label>
      {proposed.length > 0 ? (
        <div className="mb-3 flex items-center justify-between">
          <span className="text-[12px] text-muted-foreground">{proposed.length} 条待确认</span>
          <Button size="sm" variant="secondary" onClick={() => void acceptAll()}>
            全部接受
          </Button>
        </div>
      ) : null}
      {store.items.length === 0 ? (
        <p className="px-1 py-6 text-center text-[12px] text-muted-foreground">
          还没有记忆。完成一轮对话后，相关条目会出现在这里。
        </p>
      ) : (
        <div className="space-y-2">
          {[...proposed, ...accepted].map((item) => (
            <MemoryCard
              key={item.id}
              item={item}
              draft={drafts[item.id] ?? item.text}
              onDraft={(text) => setDrafts((current) => ({ ...current, [item.id]: text }))}
              onAccept={() => void acceptOne(item.id)}
              onSave={() => void saveText(item.id)}
              onDelete={() => void remove(item.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function MemoryCard({
  item,
  draft,
  onDraft,
  onAccept,
  onSave,
  onDelete,
}: {
  item: MemoryItem;
  draft: string;
  onDraft: (text: string) => void;
  onAccept: () => void;
  onSave: () => void;
  onDelete: () => void;
}) {
  const dirty = draft !== item.text;
  return (
    <section className="rounded-md bg-white/[0.03] px-3 py-2.5">
      <div className="mb-1.5 flex items-center gap-2 text-[11px] text-muted-foreground">
        <Bookmark className="h-3 w-3" />
        <span>{KIND_LABEL[item.kind] ?? item.kind}</span>
        <span
          className={cn(
            "rounded px-1.5 py-0.5",
            item.status === "accepted" ? "bg-emerald-500/10 text-emerald-300" : "bg-amber-500/10 text-amber-300",
          )}
        >
          {item.status === "accepted" ? "已采用" : "待确认"}
        </span>
      </div>
      <textarea
        value={draft}
        onChange={(event) => onDraft(event.target.value)}
        rows={3}
        className="w-full resize-none rounded-md bg-white/[0.04] px-2 py-1.5 text-[12.5px] leading-5 outline-none"
      />
      <div className="mt-2 flex flex-wrap gap-1">
        {item.status === "proposed" ? (
          <Button size="sm" onClick={onAccept}>
            接受
          </Button>
        ) : null}
        {dirty ? (
          <Button size="sm" variant="secondary" onClick={onSave}>
            保存修改
          </Button>
        ) : null}
        <Button size="sm" variant="ghost" onClick={onDelete}>
          删除
        </Button>
      </div>
    </section>
  );
}
