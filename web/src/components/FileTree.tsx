import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Plus } from "lucide-react";
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

export function FileTree({
  nodes,
  active,
  changed,
  onOpen,
  onCreate,
}: {
  nodes: TreeNode[];
  active: string | null;
  changed: Set<string>;
  onOpen: (path: string) => void;
  onCreate: () => void;
}) {
  const [query, setQuery] = useState("");
  const files = useMemo(() => flattenFiles(nodes), [nodes]);
  const filtered = query.trim()
    ? files.filter((path) => path.toLowerCase().includes(query.trim().toLowerCase()))
    : null;

  return (
    <div className="flex h-full min-h-0 flex-col">
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
              <button
                key={path}
                type="button"
                className={cn(
                  "block w-full truncate px-2 py-0.5 text-left hover:bg-white/5",
                  active === path && "bg-primary/15 text-primary",
                  changed.has(path) && "text-emerald-400",
                )}
                onClick={() => onOpen(path)}
              >
                {path}
              </button>
            ))
          )
        ) : (
          nodes.map((node) => (
            <TreeItem
              key={node.path || node.name}
              node={node}
              active={active}
              changed={changed}
              onOpen={onOpen}
              depth={0}
            />
          ))
        )}
      </div>
    </div>
  );
}

function TreeItem({
  node,
  active,
  changed,
  onOpen,
  depth,
}: {
  node: TreeNode;
  active: string | null;
  changed: Set<string>;
  onOpen: (path: string) => void;
  depth: number;
}) {
  const [open, setOpen] = useState(depth < 1);
  if (node.kind === "dir") {
    return (
      <div>
        <button
          type="button"
          className="flex w-full items-center gap-1 rounded px-1 py-0.5 text-left hover:bg-white/5"
          style={{ paddingLeft: 8 + depth * 12 }}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          <span className="truncate">{node.name}</span>
        </button>
        {open
          ? node.children.map((child) => (
              <TreeItem
                key={child.path || child.name}
                node={child}
                active={active}
                changed={changed}
                onOpen={onOpen}
                depth={depth + 1}
              />
            ))
          : null}
      </div>
    );
  }
  return (
    <button
      type="button"
      className={cn(
        "block w-full truncate rounded px-1 py-0.5 text-left hover:bg-white/5",
        active === node.path && "bg-primary/15 text-primary",
        changed.has(node.path) && !active && "text-emerald-400",
      )}
      style={{ paddingLeft: 20 + depth * 12 }}
      onClick={() => onOpen(node.path)}
    >
      {node.name}
    </button>
  );
}
