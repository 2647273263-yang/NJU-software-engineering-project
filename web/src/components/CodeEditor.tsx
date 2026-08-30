import { ClipboardEvent, KeyboardEvent, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { applyHunkUndo, type ChangeBlock } from "../lib/diffHunks";
import { highlightCode, highlightLine } from "../lib/syntax";
import { cn } from "../lib/utils";

export type EditorSelection = {
  startLine: number;
  endLine: number;
  text: string;
};

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

function selectionOf(value: string, start: number, end: number): EditorSelection | null {
  if (start === end) return null;
  const from = Math.min(start, end);
  const to = Math.max(start, end);
  const text = value.slice(from, to);
  if (!text.trim()) return null;
  return {
    startLine: value.slice(0, from).split("\n").length,
    endLine: value.slice(0, to).split("\n").length,
    text,
  };
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function FindBar({
  query,
  setQuery,
  replace,
  setReplace,
  matchIndex,
  matchCount,
  onJump,
  onReplace,
  onReplaceAll,
  onClose,
}: {
  query: string;
  setQuery: (value: string) => void;
  replace: string;
  setReplace: (value: string) => void;
  matchIndex: number;
  matchCount: number;
  onJump: (index: number) => void;
  onReplace: () => void;
  onReplaceAll: () => void;
  onClose: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-border px-2 py-1">
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
        className="h-7 min-w-[8rem] flex-1 rounded border border-border bg-transparent px-2 text-[12px] outline-none"
      />
      <input
        value={replace}
        placeholder="替换为"
        onChange={(event) => setReplace(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            onReplace();
          }
          if (event.key === "Escape") onClose();
        }}
        className="h-7 min-w-[8rem] flex-1 rounded border border-border bg-transparent px-2 text-[12px] outline-none"
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
      <button type="button" className="text-[11px] text-muted-foreground" onClick={onReplace}>
        替换
      </button>
      <button type="button" className="text-[11px] text-muted-foreground" onClick={onReplaceAll}>
        全部替换
      </button>
    </div>
  );
}

export function CodeEditor({
  path,
  value,
  onChange,
  hunks,
  onSave,
  onHunkDo,
  onHunkUndo,
  onAskSelection,
}: {
  path: string;
  value: string;
  onChange: (next: string) => void;
  hunks: ChangeBlock[];
  onSave: () => void;
  onHunkDo: (id: string) => void;
  onHunkUndo: (id: string, next: string) => void;
  onAskSelection: (selection: EditorSelection) => void;
}) {
  const lines = useMemo(() => value.split("\n"), [value]);
  const rows = useMemo(() => buildRows(value, hunks), [value, hunks]);
  const highlighted = useMemo(() => highlightCode(value, path), [value, path]);
  const editor = useRef<HTMLTextAreaElement>(null);
  const gutter = useRef<HTMLDivElement>(null);
  const highlight = useRef<HTMLPreElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  const [replace, setReplace] = useState("");
  const [findOpen, setFindOpen] = useState(false);
  const [matchIndex, setMatchIndex] = useState(0);
  const [selection, setSelection] = useState<EditorSelection | null>(null);
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
    if (highlight.current && editor.current) {
      highlight.current.scrollTop = editor.current.scrollTop;
      highlight.current.scrollLeft = editor.current.scrollLeft;
    }
  }

  function captureSelection(start: number, end: number) {
    setSelection(selectionOf(value, start, end));
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
    syncScroll();
  }

  function openFind() {
    setFindOpen(true);
  }

  function replaceCurrent() {
    if (!query || matches.length === 0) return;
    const start = matches[matchIndex] ?? 0;
    const source = value.slice(start, start + query.length);
    const next = `${value.slice(0, start)}${replace}${value.slice(start + source.length)}`;
    onChange(next);
  }

  function replaceAll() {
    if (!query) return;
    onChange(value.replace(new RegExp(escapeRegExp(query), "gi"), replace));
  }

  function onPlainKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      onSave();
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") {
      event.preventDefault();
      openFind();
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "h") {
      event.preventDefault();
      openFind();
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
      openFind();
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "h") {
      event.preventDefault();
      openFind();
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

  const ask =
    selection && onAskSelection ? (
      <div className="flex items-center gap-2 border-b border-border bg-primary/10 px-2 py-1 text-[12px]">
        <span className="min-w-0 flex-1 truncate text-muted-foreground">
          已选 {selection.startLine === selection.endLine
            ? `第 ${selection.startLine} 行`
            : `第 ${selection.startLine}–${selection.endLine} 行`}
        </span>
        <button
          type="button"
          className="rounded px-1.5 py-0.5 text-primary hover:bg-white/5"
          onClick={() => onAskSelection(selection)}
        >
          向 Agent 提问
        </button>
      </div>
    ) : null;

  const find = findOpen ? (
    <FindBar
      query={query}
      setQuery={setQuery}
      replace={replace}
      setReplace={setReplace}
      matchIndex={matchIndex}
      matchCount={matches.length}
      onJump={jumpTo}
      onReplace={replaceCurrent}
      onReplaceAll={replaceAll}
      onClose={() => setFindOpen(false)}
    />
  ) : null;

  if (!decorated) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        {ask}
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
          <div className="relative min-h-0 min-w-0 flex-1">
            <pre
              ref={highlight}
              aria-hidden
              className="code-highlight pointer-events-none absolute inset-0 overflow-auto whitespace-pre-wrap break-words p-2 font-mono text-[12px] leading-5"
              dangerouslySetInnerHTML={{ __html: highlighted || " " }}
            />
            <textarea
              ref={editor}
              value={value}
              onChange={(event) => onChange(event.target.value)}
              onScroll={syncScroll}
              onKeyDown={onPlainKey}
              onSelect={(event) =>
                captureSelection(event.currentTarget.selectionStart, event.currentTarget.selectionEnd)
              }
              onKeyUp={(event) =>
                captureSelection(event.currentTarget.selectionStart, event.currentTarget.selectionEnd)
              }
              onMouseUp={(event) =>
                captureSelection(event.currentTarget.selectionStart, event.currentTarget.selectionEnd)
              }
              spellCheck={false}
              wrap="soft"
              className="code-input absolute inset-0 z-10 min-h-0 min-w-0 resize-none overflow-auto bg-transparent p-2 font-mono text-[12px] leading-5 outline-none"
            />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {ask}
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
                <pre
                  className="code-highlight min-w-0 flex-1 whitespace-pre-wrap px-2 py-0 text-red-200/80"
                  dangerouslySetInnerHTML={{ __html: highlightLine(row.text, path) || " " }}
                />
              </div>
            );
          }
          return (
            <div
              key={`ln-${row.line}-${index}`}
              data-file-line={row.line}
              className={cn("relative flex", row.mark === "add" && "bg-emerald-400/[0.10]")}
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
              <div className="relative min-w-0 flex-1">
                <pre
                  aria-hidden
                  className={cn(
                    "code-highlight pointer-events-none absolute inset-0 whitespace-pre-wrap px-2 py-0",
                    row.mark === "add" && "text-emerald-50/90",
                  )}
                  dangerouslySetInnerHTML={{ __html: highlightLine(row.text, path) || " " }}
                />
                <textarea
                  value={row.text}
                  rows={1}
                  spellCheck={false}
                  onChange={(event) => replaceLine(row.line, event.target.value)}
                  onKeyDown={(event) => onLineKey(event, row.line, row.text)}
                  onPaste={(event) => onLinePaste(event, row.line, row.text)}
                  onSelect={(event) => {
                    const start = event.currentTarget.selectionStart;
                    const end = event.currentTarget.selectionEnd;
                    if (start === end) {
                      setSelection(null);
                      return;
                    }
                    const from = Math.min(start, end);
                    const to = Math.max(start, end);
                    setSelection({
                      startLine: row.line,
                      endLine: row.line,
                      text: row.text.slice(from, to),
                    });
                  }}
                  onInput={(event) => {
                    const node = event.currentTarget;
                    node.style.height = "auto";
                    node.style.height = `${node.scrollHeight}px`;
                  }}
                  className="code-input relative z-10 min-w-0 w-full flex-1 resize-none overflow-hidden bg-transparent px-2 py-0 outline-none"
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
