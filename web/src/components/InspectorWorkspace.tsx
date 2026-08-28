import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Check, FileCode, GitBranch, Layers, Plus, TerminalSquare, X } from "lucide-react";
import { cn } from "../lib/utils";
import { SplitHandle } from "./SplitHandle";

export type PanelKind = "files" | "evidence" | "context" | "terminal" | "git";

export type InspectorPage = {
  id: string;
  kind: PanelKind;
};

export type InspectorHandle = {
  open: (kind: PanelKind) => void;
};

const PANEL_OPTIONS: { kind: PanelKind; label: string; icon: typeof FileCode }[] = [
  { kind: "files", label: "文件", icon: FileCode },
  { kind: "git", label: "版本", icon: GitBranch },
  { kind: "evidence", label: "核对", icon: Check },
  { kind: "context", label: "上下文", icon: Layers },
  { kind: "terminal", label: "终端", icon: TerminalSquare },
];

const PAGES_KEY = "forge-agent-inspector-pages";

type PagesState = {
  top: InspectorPage[];
  topActive: string;
  bottom: InspectorPage[];
  bottomActive: string;
  focus: "top" | "bottom";
};

function newId(kind: PanelKind) {
  return `${kind}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function defaultPages(): PagesState {
  const id = "files-default";
  return { top: [{ id, kind: "files" }], topActive: id, bottom: [], bottomActive: "", focus: "top" };
}

function loadPages(): PagesState {
  const fallback = defaultPages();
  try {
    const raw = localStorage.getItem(PAGES_KEY);
    if (!raw) return fallback;
    const parsed = { ...fallback, ...JSON.parse(raw) } as PagesState;
    const valid = new Set(PANEL_OPTIONS.map((item) => item.kind));
    parsed.top = parsed.top.filter((page) => valid.has(page.kind));
    parsed.bottom = parsed.bottom.filter((page) => valid.has(page.kind));
    if (parsed.top.length === 0 && parsed.bottom.length === 0) return fallback;
    if (!parsed.top.some((page) => page.id === parsed.topActive)) {
      parsed.topActive = parsed.top[0]?.id ?? "";
    }
    if (!parsed.bottom.some((page) => page.id === parsed.bottomActive)) {
      parsed.bottomActive = parsed.bottom[0]?.id ?? "";
    }
    return parsed;
  } catch {
    return fallback;
  }
}

function savePages(state: PagesState) {
  localStorage.setItem(PAGES_KEY, JSON.stringify(state));
}

function IconOf(kind: PanelKind) {
  return PANEL_OPTIONS.find((item) => item.kind === kind)?.icon ?? FileCode;
}

function pageTitle(page: InspectorPage, all: InspectorPage[]) {
  if (page.kind !== "terminal") {
    return PANEL_OPTIONS.find((item) => item.kind === page.kind)?.label ?? page.kind;
  }
  const terminals = all.filter((item) => item.kind === "terminal");
  if (terminals.length <= 1) return "终端";
  const index = terminals.findIndex((item) => item.id === page.id);
  return `终端 ${index + 1}`;
}

function TabStrip({
  pages,
  allPages,
  active,
  menu,
  onSelect,
  onClose,
  onDragStart,
  onDropTab,
}: {
  pages: InspectorPage[];
  allPages: InspectorPage[];
  active: string;
  menu?: ReactNode;
  onSelect: (id: string) => void;
  onClose: (id: string) => void;
  onDragStart: (id: string) => void;
  onDropTab: () => void;
}) {
  return (
    <div
      className="flex h-8 shrink-0 items-center gap-0.5 border-b border-white/[0.06] bg-[#121212] px-1"
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        onDropTab();
      }}
    >
      {menu}
      <div className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto">
        {pages.map((page) => {
          const Icon = IconOf(page.kind);
          const selected = active === page.id;
          return (
            <div
              key={page.id}
              draggable
              onDragStart={(event) => {
                event.dataTransfer.setData("text/plain", page.id);
                event.dataTransfer.effectAllowed = "move";
                onDragStart(page.id);
              }}
              className={cn(
                "group flex h-6 max-w-[260px] shrink-0 items-center rounded-md pl-1.5 pr-0.5",
                selected ? "bg-white/[0.08] text-foreground" : "text-muted-foreground hover:bg-white/[0.04]",
              )}
            >
              <button
                type="button"
                className="flex min-w-0 flex-1 items-center gap-1.5 text-left text-[12px]"
                onClick={() => onSelect(page.id)}
              >
                <Icon className="h-3 w-3 shrink-0 opacity-70" />
                <span className="truncate">{pageTitle(page, allPages)}</span>
              </button>
              <button
                type="button"
                className={cn(
                  "ml-0.5 rounded p-0.5 hover:bg-white/10 hover:text-foreground",
                  selected ? "text-muted-foreground" : "text-transparent group-hover:text-muted-foreground",
                )}
                title="关闭"
                onClick={() => onClose(page.id)}
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export const InspectorWorkspace = forwardRef<
  InspectorHandle,
  {
    bottomHeight: number;
    onBottomResize: (clientY: number) => void;
    onSplit: (halfHeight: number) => void;
    render: (page: InspectorPage) => ReactNode;
  }
>(function InspectorWorkspace({ bottomHeight, onBottomResize, onSplit, render }, ref) {
  const [pages, setPages] = useState<PagesState>(loadPages);
  const [menuOpen, setMenuOpen] = useState(false);
  const [dragging, setDragging] = useState<string | null>(null);
  const [overBottom, setOverBottom] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const pagesRef = useRef(pages);
  pagesRef.current = pages;

  useEffect(() => {
    savePages(pages);
  }, [pages]);

  useEffect(() => {
    if (!menuOpen) return;
    const close = () => setMenuOpen(false);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [menuOpen]);

  useEffect(() => {
    const up = () => {
      setDragging(null);
      setOverBottom(false);
    };
    window.addEventListener("dragend", up);
    return () => window.removeEventListener("dragend", up);
  }, []);

  const allPages = useMemo(() => [...pages.top, ...pages.bottom], [pages.top, pages.bottom]);
  const split = pages.bottom.length > 0;

  function findPage(id: string) {
    return allPages.find((page) => page.id === id) ?? null;
  }

  function activate(id: string) {
    setPages((current) => {
      if (current.top.some((page) => page.id === id)) {
        return { ...current, topActive: id, focus: "top" };
      }
      if (current.bottom.some((page) => page.id === id)) {
        return { ...current, bottomActive: id, focus: "bottom" };
      }
      return current;
    });
  }

  function openKind(kind: PanelKind) {
    setMenuOpen(false);
    setPages((current) => {
      const allowMany = kind === "terminal";
      if (!allowMany) {
        const existing = [...current.top, ...current.bottom].find((page) => page.kind === kind);
        if (existing) {
          if (current.top.some((page) => page.id === existing.id)) {
            return { ...current, topActive: existing.id, focus: "top" };
          }
          return { ...current, bottomActive: existing.id, focus: "bottom" };
        }
      }
      const page: InspectorPage = { id: newId(kind), kind };
      if (current.focus === "bottom" && current.bottom.length > 0) {
        return { ...current, bottom: [...current.bottom, page], bottomActive: page.id };
      }
      const top = current.top.length ? [...current.top, page] : [page];
      return { ...current, top, topActive: page.id, focus: "top" };
    });
  }

  useImperativeHandle(ref, () => ({ open: openKind }));

  function closePage(id: string) {
    setPages((current) => {
      const top = current.top.filter((page) => page.id !== id);
      const bottom = current.bottom.filter((page) => page.id !== id);
      return {
        top,
        bottom,
        topActive: current.topActive === id ? (top[top.length - 1]?.id ?? "") : current.topActive,
        bottomActive: current.bottomActive === id ? (bottom[bottom.length - 1]?.id ?? "") : current.bottomActive,
        focus: bottom.length === 0 ? "top" : current.focus,
      };
    });
  }

  function moveTo(id: string, target: "top" | "bottom") {
    const page = findPage(id);
    if (!page) return;
    setPages((current) => {
      const top = current.top.filter((item) => item.id !== id);
      const bottom = current.bottom.filter((item) => item.id !== id);
      if (target === "bottom") {
        const nextBottom = [...bottom, page];
        return {
          top,
          bottom: nextBottom,
          topActive: current.topActive === id ? (top[top.length - 1]?.id ?? "") : current.topActive,
          bottomActive: page.id,
          focus: "bottom",
        };
      }
      const nextTop = [...top, page];
      return {
        top: nextTop,
        bottom,
        topActive: page.id,
        bottomActive: current.bottomActive === id ? (bottom[bottom.length - 1]?.id ?? "") : current.bottomActive,
        focus: "top",
      };
    });
  }

  function onDropPane(target: "top" | "bottom") {
    if (!dragging) return;
    const creating = target === "bottom" && pagesRef.current.bottom.length === 0;
    moveTo(dragging, target);
    if (creating) {
      const height = box.current?.getBoundingClientRect().height ?? 400;
      onSplit(Math.max(120, Math.round(height / 2)));
    }
    setDragging(null);
    setOverBottom(false);
  }

  const menu = (
    <div className="relative shrink-0">
      <button
        type="button"
        className="flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground hover:bg-white/[0.08] hover:text-foreground"
        title="打开页面"
        onClick={(event) => {
          event.stopPropagation();
          setMenuOpen((value) => !value);
        }}
      >
        <Plus className="h-3.5 w-3.5" />
      </button>
      {menuOpen ? (
        <div
          className="absolute top-full left-0 z-30 mt-1 w-44 overflow-hidden rounded-lg border border-border bg-[#1a1a1a] py-1 shadow-xl"
          onClick={(event) => event.stopPropagation()}
        >
          {PANEL_OPTIONS.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.kind}
                type="button"
                className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[12px] hover:bg-white/5"
                onClick={() => openKind(item.kind)}
              >
                <Icon className="h-3.5 w-3.5 text-muted-foreground" />
                <span className="flex-1">{item.label}</span>
                {item.kind === "terminal" ? (
                  <span className="text-[10px] text-muted-foreground">可多开</span>
                ) : null}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );

  function paneBody(list: InspectorPage[], active: string, which: "top" | "bottom") {
    return (
      <div
        className="relative flex min-h-0 flex-1 flex-col"
        onMouseDown={() => setPages((current) => ({ ...current, focus: which }))}
      >
        {list.length === 0 ? (
          <div className="flex h-full items-center justify-center px-4 text-center text-[12px] text-muted-foreground">
            点左侧 + 打开一个页面，或把标签拖到这里。
          </div>
        ) : (
          list.map((page) => (
            <div
              key={page.id}
              className={cn("min-h-0", page.id === active ? "flex min-h-0 flex-1 flex-col" : "hidden")}
            >
              {render(page)}
            </div>
          ))
        )}
      </div>
    );
  }

  return (
    <div
      ref={box}
      className="relative flex min-h-0 flex-1 flex-col"
      onDragOver={(event) => {
        if (!dragging) return;
        event.preventDefault();
        const rect = box.current?.getBoundingClientRect();
        if (!rect) return;
        setOverBottom(event.clientY > rect.top + rect.height * 0.55);
      }}
    >
      <TabStrip
        pages={pages.top}
        allPages={allPages}
        active={pages.topActive}
        menu={menu}
        onSelect={activate}
        onClose={closePage}
        onDragStart={setDragging}
        onDropTab={() => onDropPane("top")}
      />
      {paneBody(pages.top, pages.topActive, "top")}
      {split ? (
        <>
          <SplitHandle axis="y" onMove={onBottomResize} />
          <div className="flex min-h-0 shrink-0 flex-col" style={{ height: Math.max(80, bottomHeight) }}>
            <TabStrip
              pages={pages.bottom}
              allPages={allPages}
              active={pages.bottomActive}
              onSelect={activate}
              onClose={closePage}
              onDragStart={setDragging}
              onDropTab={() => onDropPane("bottom")}
            />
            {paneBody(pages.bottom, pages.bottomActive, "bottom")}
          </div>
        </>
      ) : null}
      {dragging && !split && overBottom ? (
        <div
          className="absolute inset-x-0 bottom-0 z-20 flex items-center justify-center border-t border-dashed border-primary/40 bg-primary/10 text-[12px] text-primary"
          style={{ height: "45%" }}
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            onDropPane("bottom");
          }}
        >
          放到这里，在右侧下方分栏显示
        </div>
      ) : null}
    </div>
  );
});
