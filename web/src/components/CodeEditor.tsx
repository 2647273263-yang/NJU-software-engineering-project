import { ClipboardEvent, KeyboardEvent, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { applyHunkUndo, type ChangeBlock } from "../lib/diffHunks";
import { cn } from "../lib/utils";

type ViewRow =
  | { type: "code"; line: number; text: string; mark: "none" | "add" }
  | { type: "del"; text: string; hunkId: string }
  | { type: "bar"; hunkId: string };

function buildRows(value: string, hunks: ChangeBlock[]): ViewRow[] {
  const lines = value.split("\n");
  const rows: ViewRow[] = [];
  let index = 0;
  const ordered = [...hunks].sort((left, right) => left.newStart - right.newStart);
  for (const hunk of ordered) {
    const start = Math.max(0, hunk.newStart - 1);
    while (index < start && index < lines.length) {
      rows.push({ type: "code", line: index + 1, text: lines[index] ?? "", mark: "none" });
      index += 1;
    }
    for (const text of hunk.deleted) {
      rows.push({ type: "del", text, hunkId: hunk.id });
    }
    for (let i = 0; i < hunk.added.length; i += 1) {
      rows.push({
        type: "code",
        line: index + 1,
        text: lines[index] ?? "",
        mark: "add",
      });
      index += 1;
    }
    rows.push({ type: "bar", hunkId: hunk.id });
  }
  while (index < lines.length) {
    rows.push({ type: "code", line: index + 1, text: lines[index] ?? "", mark: "none" });
    index += 1;
  }
  return rows;
}

function FindBar({
  query,
  setQuery,
  matchIndex,
  matchCount,
  onJump,
  onClose,
}: {
  query: string;
  setQuery: (value: string) => void;
  matchIndex: number;
  matchCount: number;
  onJump: (index: number) => void;
  onClose: () => void;
}) {
  return (
    <div className="flex items-center gap-2 border-b border-border px-2 py-1">
      <input
        autoFocus
        value={query}
        placeholder="查找"
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            onJump(matchIndex + (event.shiftKey ? -1 : 1));
          }
          if (event.key === "Escape") onClose();
        }}
        className="h-7 min-w-0 flex-1 rounded border border-border bg-transparent px-2 text-[12px] outline-none"
      />
      <span className="shrink-0 text-[11px] text-muted-foreground">
        {matchCount ? `${matchIndex + 1}/${matchCount}` : "无匹配"}
      </span>
      <button type="button" className="text-[11px] text-muted-foreground" onClick={() => onJump(matchIndex - 1)}>
        上一个
      </button>
      <button type="button" className="text-[11px] text-muted-foreground" onClick={() => onJump(matchIndex + 1)}>
        下一个
      </button>
    </div>
  );
}

export function CodeEditor({
  value,
  onChange,
  hunks,
  onSave,
  onHunkDo,
  onHunkUndo,
}: {
  value: string;
  onChange: (next: string) => void;
  hunks: ChangeBlock[];
  onSave: () => void;
  onHunkDo: (id: string) => void;
  onHunkUndo: (id: string, next: string) => void;
}) {
  const lines = useMemo(() => value.split("\n"), [value]);
  const rows = useMemo(() => buildRows(value, hunks), [value, hunks]);
  const editor = useRef<HTMLTextAreaElement>(null);
  const gutter = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  const [findOpen, setFindOpen] = useState(false);
  const [matchIndex, setMatchIndex] = useState(0);
  const gutterWidth = `${Math.max(2, String(lines.length).length) + 1}ch`;
  const decorated = hunks.length > 0;

  const matches = useMemo(() => {
    if (!query) return [] as number[];
    const found: number[] = [];
    const needle = query.toLowerCase();
    const hay = value.toLowerCase();
    let from = 0;
    while (from < hay.length) {
      const at = hay.indexOf(needle, from);
      if (at < 0) break;
      found.push(at);
      from = at + Math.max(needle.length, 1);
    }
    return found;
  }, [query, value]);

  useEffect(() => {
    setMatchIndex(0);
  }, [query, value]);

  useLayoutEffect(() => {
    if (!decorated || !scroller.current) return;
    scroller.current.querySelectorAll("textarea").forEach((node) => {
      const area = node as HTMLTextAreaElement;
      area.style.height = "auto";
      area.style.height = `${area.scrollHeight}px`;
    });
  }, [decorated, value, hunks]);

  function syncScroll() {
    if (gutter.current && editor.current) {
      gutter.current.scrollTop = editor.current.scrollTop;
    }
  }

  function jumpTo(index: number) {
    if (matches.length === 0) return;
    const next = ((index % matches.length) + matches.length) % matches.length;
    setMatchIndex(next);
    const start = matches[next] ?? 0;
    const before = value.slice(0, start);
    const line = before.split("\n").length;
    if (decorated) {
      const node = scroller.current?.querySelector(`[data-file-line="${line}"]`);
      node?.scrollIntoView({ block: "center" });
      return;
    }
    const node = editor.current;
    if (!node) return;
    node.focus();
    node.setSelectionRange(start, start + query.length);
    node.scrollTop = Math.max(0, (line - 4) * 20);
  }

  function onPlainKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      onSave();
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") {
      event.preventDefault();
      setFindOpen(true);
    }
  }

  function replaceLine(fileLine: number, nextText: string) {
    const next = [...lines];
    next[fileLine - 1] = nextText;
    onChange(next.join("\n"));
  }

  function onLineKey(event: KeyboardEvent<HTMLTextAreaElement>, fileLine: number, text: string) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      onSave();
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") {
      event.preventDefault();
      setFindOpen(true);
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      const at = event.currentTarget.selectionStart ?? text.length;
      const next = [...lines];
      next.splice(fileLine - 1, 1, text.slice(0, at), text.slice(at));
      onChange(next.join("\n"));
      return;
    }
    if (event.key === "Backspace" && (event.currentTarget.selectionStart ?? 0) === 0 && fileLine > 1) {
      event.preventDefault();
      const next = [...lines];
      const merged = `${next[fileLine - 2] ?? ""}${text}`;
      next.splice(fileLine - 2, 2, merged);
      onChange(next.join("\n"));
    }
  }

  function onLinePaste(event: ClipboardEvent<HTMLTextAreaElement>, fileLine: number, text: string) {
    const pasted = event.clipboardData.getData("text");
    if (!pasted.includes("\n")) return;
    event.preventDefault();
    const at = event.currentTarget.selectionStart ?? text.length;
    const end = event.currentTarget.selectionEnd ?? at;
    const merged = `${text.slice(0, at)}${pasted}${text.slice(end)}`;
    const next = [...lines];
    next.splice(fileLine - 1, 1, ...merged.split("\n"));
    onChange(next.join("\n"));
  }

  const find = findOpen ? (
    <FindBar
      query={query}
      setQuery={setQuery}
      matchIndex={matchIndex}
      matchCount={matches.length}
      onJump={jumpTo}
      onClose={() => setFindOpen(false)}
    />
  ) : null;

  if (!decorated) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        {find}
        <div className="flex min-h-0 flex-1 overflow-hidden font-mono text-[12px] leading-5">
          <div
            ref={gutter}
            className="shrink-0 overflow-hidden border-r border-white/5 bg-[#121212] py-2 pr-2 text-right text-muted-foreground"
            style={{ width: gutterWidth }}
          >
            {lines.map((_, index) => (
              <div key={`n-${index}`}>{index + 1}</div>
            ))}
          </div>
          <textarea
            ref={editor}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            onScroll={syncScroll}
            onKeyDown={onPlainKey}
            spellCheck={false}
            wrap="soft"
            className="min-h-0 min-w-0 flex-1 resize-none bg-transparent p-2 outline-none"
          />
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {find}
      <div ref={scroller} className="min-h-0 flex-1 overflow-auto font-mono text-[12px] leading-5">
        {rows.map((row, index) => {
          if (row.type === "bar") {
            return (
              <div
                key={`bar-${row.hunkId}-${index}`}
                className="flex items-center gap-1 border-b border-white/5 py-1 pl-2"
                style={{ paddingLeft: `calc(${gutterWidth} + 0.75rem)` }}
              >
                <button
                  type="button"
                  className="rounded border border-red-400/25 bg-red-500/15 px-1.5 py-0.5 text-[10px] font-medium tracking-wide text-red-300/90 hover:bg-red-500/25"
                  title="只撤销这一块 Agent 修改"
                  onClick={() => {
                    const hunk = hunks.find((item) => item.id === row.hunkId);
                    if (!hunk) return;
                    onHunkUndo(row.hunkId, applyHunkUndo(value, hunk));
                  }}
                >
                  UNDO
                </button>
                <button
                  type="button"
                  className="rounded border border-emerald-400/25 bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium tracking-wide text-emerald-300/90 hover:bg-emerald-500/25"
                  title="接受这一块，去掉红绿对比（改动已在磁盘上）"
                  onClick={() => onHunkDo(row.hunkId)}
                >
                  DO
                </button>
              </div>
            );
          }
          if (row.type === "del") {
            return (
              <div key={`del-${row.hunkId}-${index}`} className="flex bg-red-400/[0.10]">
                <div
                  className="shrink-0 border-r border-red-400/10 py-0 pr-2 text-right text-red-300/50"
                  style={{ width: gutterWidth }}
                >
                  −
                </div>
                <pre className="min-w-0 flex-1 whitespace-pre-wrap px-2 py-0 text-red-200/80">{row.text || " "}</pre>
              </div>
            );
          }
          return (
            <div
              key={`ln-${row.line}-${index}`}
              data-file-line={row.line}
              className={cn("flex", row.mark === "add" && "bg-emerald-400/[0.10]")}
            >
              <div
                className={cn(
                  "shrink-0 border-r border-white/5 py-0 pr-2 text-right text-muted-foreground",
                  row.mark === "add" && "border-emerald-400/10 text-emerald-400/70",
                )}
                style={{ width: gutterWidth }}
              >
                {row.line}
              </div>
              <textarea
                value={row.text}
                rows={1}
                spellCheck={false}
                onChange={(event) => replaceLine(row.line, event.target.value)}
                onKeyDown={(event) => onLineKey(event, row.line, row.text)}
                onPaste={(event) => onLinePaste(event, row.line, row.text)}
                onInput={(event) => {
                  const node = event.currentTarget;
                  node.style.height = "auto";
                  node.style.height = `${node.scrollHeight}px`;
                }}
                className={cn(
                  "min-w-0 flex-1 resize-none overflow-hidden bg-transparent px-2 py-0 outline-none",
                  row.mark === "add" && "text-emerald-50/90",
                )}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}
