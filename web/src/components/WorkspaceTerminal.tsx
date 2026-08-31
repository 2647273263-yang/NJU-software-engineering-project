import { useEffect, useRef } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";

const windowsPty = /Windows/i.test(navigator.userAgent)
  ? { backend: "conpty" as const, buildNumber: 19041 }
  : undefined;

const MIN_COLS = 48;
const MIN_ROWS = 12;

export function WorkspaceTerminal({
  workspace,
  sessionId,
}: {
  workspace: string;
  sessionId: string;
}) {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = host.current;
    if (!node || !workspace) return;
    let cancelled = false;
    let socket: WebSocket | null = null;
    let dataSub: { dispose: () => void } | null = null;
    let resizeSub: { dispose: () => void } | null = null;
    let frame = 0;

    const term = new Terminal({
      cols: 80,
      rows: 24,
      cursorBlink: true,
      fontSize: 12,
      fontFamily: 'Consolas, "Cascadia Mono", "Courier New", monospace',
      theme: {
        background: "#101218",
        foreground: "#d5dae3",
        cursor: "#d5dae3",
        cursorAccent: "#101218",
      },
      scrollback: 5000,
      convertEol: false,
      windowsPty,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(node);

    function layoutReady() {
      return node !== null && node.clientWidth >= 160 && node.clientHeight >= 80;
    }

    function fitIfReady() {
      if (!layoutReady()) return false;
      try {
        fit.fit();
      } catch {
        return false;
      }
      return term.cols >= MIN_COLS && term.rows >= MIN_ROWS;
    }

    function sendSize() {
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    }

    function connect() {
      if (cancelled || socket) return;
      if (!fitIfReady()) {
        term.resize(Math.max(term.cols, 80), Math.max(term.rows, 24));
      }
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      const params = new URLSearchParams({
        workspace,
        session: sessionId,
        cols: String(term.cols),
        rows: String(term.rows),
      });
      socket = new WebSocket(`${protocol}://${location.host}/api/workspace/terminal/ws?${params.toString()}`);
      socket.addEventListener("open", () => {
        if (cancelled) return;
        fitIfReady();
        sendSize();
        term.focus();
      });
      socket.addEventListener("message", (event) => {
        if (typeof event.data === "string") {
          term.write(event.data);
          return;
        }
        term.write(new Uint8Array(event.data as ArrayBuffer));
      });
      dataSub = term.onData((data) => {
        if (socket?.readyState === WebSocket.OPEN) socket.send(data);
      });
      resizeSub = term.onResize(({ cols, rows }) => {
        if (socket?.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "resize", cols, rows }));
        }
      });
    }

    function waitThenConnect() {
      if (cancelled) return;
      if (fitIfReady() || frame >= 45) {
        connect();
        return;
      }
      frame += 1;
      requestAnimationFrame(waitThenConnect);
    }

    const observer = new ResizeObserver(() => {
      if (!layoutReady()) return;
      try {
        fit.fit();
      } catch {
        /* container may be hidden */
      }
    });
    observer.observe(node);
    requestAnimationFrame(waitThenConnect);

    return () => {
      cancelled = true;
      observer.disconnect();
      dataSub?.dispose();
      resizeSub?.dispose();
      socket?.close();
      term.dispose();
    };
  }, [workspace, sessionId]);

  if (!workspace) {
    return <p className="p-3 text-[12px] text-muted-foreground">请先选择工作区，再使用终端。</p>;
  }

  return <div ref={host} className="workspace-terminal h-full min-h-0 w-full" />;
}
