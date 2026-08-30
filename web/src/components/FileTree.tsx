import { useEffect, useMemo, useState, type MouseEvent } from "react";
import { ChevronDown, ChevronRight, Pencil, Plus, Trash2 } from "lucide-react";
import { Button } from "./ui/button";
import { cn } from "../lib/utils";
import type { TreeNode } from "../lib/types";

export function flattenFiles(nodes: TreeNode[]): string[] {
  const paths: string[] = [];
  for (const node of nodes) {
    if (node.kind === "file") paths.push(node.path);
    else paths.push(...flattenFiles(node.children));
  }
  return paths;
}

function parentOf(path: string): string {
  const parts = path.replaceAll("\\", "/").split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}

function joinPath(parent: string, name: string): string {
  const clean = name.replaceAll("\\", "/").replace(/^\/+/, "").trim();
  return parent ? `${parent}/${clean}` : clean;
}

export function FileTree({
  nodes,
  active,
  changed,
  onOpen,
  onCreate,
  onRename,
  onDelete,
}: {
  nodes: TreeNode[];
  active: string | null;
  changed: Set<string>;
  onOpen: (path: string) => void;
  onCreate: () => void;
  onRename: (from: string, to: string) => void;
  onDelete: (path: string, kind: "file" | "dir") => void;
}) {
  const [query, setQuery] = useState("");
  const [menu, setMenu] = useState<{ x: number; y: number; node: TreeNode } | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const files = useMemo(() => flattenFiles(nodes), [nodes]);
  const filtered = query.trim()
    ? files.filter((path) => path.toLowerCase().includes(query.trim().toLowerCase()))
    : null;

  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [menu]);

  function beginRename(node: TreeNode) {
    setMenu(null);
    setRenaming(node.path);
  }

  function commitRename(from: string, name: string) {
    setRenaming(null);
    const next = name.trim();
    if (!next || next.includes("/") || next.includes("\\")) return;
    const to = joinPath(parentOf(from), next);
    if (to !== from) onRename(from, to);
  }

  return (
    <div className="relative flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-1 border-b border-border px-1 py-1">
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="筛选文件"
          className="h-7 min-w-0 flex-1 rounded border border-border bg-transparent px-2 text-[12px] outline-none"
        />
        <Button type="button" size="icon" variant="ghost" title="新建文件" onClick={onCreate}>
          <Plus className="h-3.5 w-3.5" />
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto py-1 text-[12.5px]">
        {nodes.length === 0 ? (
          <p className="px-2 text-[12px] text-muted-foreground">选择工作区后显示文件树。</p>
        ) : filtered ? (
          filtered.length === 0 ? (
            <p className="px-2 text-[12px] text-muted-foreground">没有匹配的文件。</p>
          ) : (
            filtered.map((path) => (
              <TreeRow
                key={path}
                label={path}
                path={path}
                kind="file"
                active={active === path}
                changed={changed.has(path)}
                depth={0}
                renaming={renaming === path}
                onOpen={() => onOpen(path)}
                onMenu={(event) =>
                  setMenu({
                    x: event.clientX,
                    y: event.clientY,
                    node: { name: path.split("/").pop() ?? path, path, kind: "file", children: [] },
                  })
                }
                onBeginRename={() => setRenaming(path)}
                onCommitRename={(name) => commitRename(path, name)}
                onCancelRename={() => setRenaming(null)}
                onDelete={() => onDelete(path, "file")}
              />
            ))
          )
        ) : (
          nodes.map((node) => (
            <TreeItem
              key={node.path || node.name}
              node={node}
              active={active}
              changed={changed}
              renaming={renaming}
              onOpen={onOpen}
              onMenu={(event, item) => setMenu({ x: event.clientX, y: event.clientY, node: item })}
              onBeginRename={beginRename}
              onCommitRename={commitRename}
              onCancelRename={() => setRenaming(null)}
              onDelete={onDelete}
              depth={0}
            />
          ))
        )}
      </div>
      {menu ? (
        <div
          className="fixed z-50 min-w-[132px] rounded-md border border-border bg-[#1f1f1f] py-1 text-[12px] shadow-lg"
          style={{ left: menu.x, top: menu.y }}
          onClick={(event) => event.stopPropagation()}
        >
          <button
            type="button"
            className="block w-full px-3 py-1.5 text-left hover:bg-white/5"
            onClick={() => beginRename(menu.node)}
          >
            重命名
          </button>
          <button
            type="button"
            className="block w-full px-3 py-1.5 text-left text-red-300 hover:bg-white/5"
            onClick={() => {
              setMenu(null);
              onDelete(menu.node.path, menu.node.kind);
            }}
          >
            删除
          </button>
        </div>
      ) : null}
    </div>
  );
}

function TreeItem({
  node,
  active,
  changed,
  renaming,
  onOpen,
  onMenu,
  onBeginRename,
  onCommitRename,
  onCancelRename,
  onDelete,
  depth,
}: {
  node: TreeNode;
  active: string | null;
  changed: Set<string>;
  renaming: string | null;
  onOpen: (path: string) => void;
  onMenu: (event: MouseEvent, node: TreeNode) => void;
  onBeginRename: (node: TreeNode) => void;
  onCommitRename: (from: string, name: string) => void;
  onCancelRename: () => void;
  onDelete: (path: string, kind: "file" | "dir") => void;
  depth: number;
}) {
  const [open, setOpen] = useState(depth < 1);
  if (node.kind === "dir") {
    return (
      <div>
        <TreeRow
          label={node.name}
          path={node.path}
          kind="dir"
          active={false}
          changed={false}
          depth={depth}
          folderOpen={open}
          renaming={renaming === node.path}
          onOpen={() => setOpen((value) => !value)}
          onMenu={(event) => onMenu(event, node)}
          onBeginRename={() => onBeginRename(node)}
          onCommitRename={(name) => onCommitRename(node.path, name)}
          onCancelRename={onCancelRename}
          onDelete={() => onDelete(node.path, "dir")}
        />
        {open
          ? node.children.map((child) => (
              <TreeItem
                key={child.path || child.name}
                node={child}
                active={active}
                changed={changed}
                renaming={renaming}
                onOpen={onOpen}
                onMenu={onMenu}
                onBeginRename={onBeginRename}
                onCommitRename={onCommitRename}
                onCancelRename={onCancelRename}
                onDelete={onDelete}
                depth={depth + 1}
              />
            ))
          : null}
      </div>
    );
  }
  return (
    <TreeRow
      label={node.name}
      path={node.path}
      kind="file"
      active={active === node.path}
      changed={changed.has(node.path)}
      depth={depth}
      renaming={renaming === node.path}
      onOpen={() => onOpen(node.path)}
      onMenu={(event) => onMenu(event, node)}
      onBeginRename={() => onBeginRename(node)}
      onCommitRename={(name) => onCommitRename(node.path, name)}
      onCancelRename={onCancelRename}
      onDelete={() => onDelete(node.path, "file")}
    />
  );
}

function TreeRow({
  label,
  path,
  kind,
  active,
  changed,
  depth,
  folderOpen,
  renaming,
  onOpen,
  onMenu,
  onBeginRename,
  onCommitRename,
  onCancelRename,
  onDelete,
}: {
  label: string;
  path: string;
  kind: "file" | "dir";
  active: boolean;
  changed: boolean;
  depth: number;
  folderOpen?: boolean;
  renaming: boolean;
  onOpen: () => void;
  onMenu: (event: MouseEvent) => void;
  onBeginRename: () => void;
  onCommitRename: (name: string) => void;
  onCancelRename: () => void;
  onDelete: () => void;
}) {
  const pad = (kind === "dir" ? 8 : 20) + depth * 12;
  return (
    <div
      className={cn(
        "group flex w-full items-center rounded px-1 py-0.5 hover:bg-white/5",
        active && "bg-primary/15 text-primary",
        changed && !active && "text-emerald-400",
      )}
      style={{ paddingLeft: pad }}
      onContextMenu={(event) => {
        event.preventDefault();
        onMenu(event);
      }}
    >
      {kind === "dir" ? (
        <button type="button" className="mr-1 shrink-0" onClick={onOpen}>
          {folderOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        </button>
      ) : null}
      {renaming ? (
        <input
          autoFocus
          defaultValue={label}
          className="h-5 min-w-0 flex-1 rounded border border-border bg-[#1a1a1a] px-1 text-[12px] outline-none"
          onClick={(event) => event.stopPropagation()}
          onBlur={(event) => {
            if (event.currentTarget.dataset.skip === "1") return;
            onCommitRename(event.currentTarget.value);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              onCommitRename(event.currentTarget.value);
            }
            if (event.key === "Escape") {
              event.preventDefault();
              event.currentTarget.dataset.skip = "1";
              onCancelRename();
            }
          }}
        />
      ) : (
        <button type="button" className="min-w-0 flex-1 truncate text-left" onClick={onOpen} title={path}>
          {label}
        </button>
      )}
      {renaming ? null : (
        <span className="hidden shrink-0 group-hover:flex">
          <button
            type="button"
            className="rounded p-0.5 text-muted-foreground hover:text-foreground"
            title="重命名"
            onClick={(event) => {
              event.stopPropagation();
              onBeginRename();
            }}
          >
            <Pencil className="h-3 w-3" />
          </button>
          <button
            type="button"
            className="rounded p-0.5 text-muted-foreground hover:text-red-300"
            title="删除"
            onClick={(event) => {
              event.stopPropagation();
              onDelete();
            }}
          >
            <Trash2 className="h-3 w-3" />
          </button>
        </span>
      )}
    </div>
  );
}
