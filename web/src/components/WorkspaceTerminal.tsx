import { useEffect, useRef } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";

const windowsPty = /Windows/i.test(navigator.userAgent)
  ? { backend: "conpty" as const, buildNumber: 19041 }
  : undefined;

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

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 12,
      fontFamily: 'Consolas, "Cascadia Mono", "Courier New", monospace',
      theme: {
        background: "#0c0c0c",
        foreground: "#cccccc",
        cursor: "#cccccc",
        cursorAccent: "#0c0c0c",
      },
      scrollback: 5000,
      convertEol: false,
      windowsPty,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(node);
    fit.fit();

    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const params = new URLSearchParams({
      workspace,
      session: sessionId,
      cols: String(term.cols),
      rows: String(term.rows),
    });
    const socket = new WebSocket(
      `${protocol}://${location.host}/api/workspace/terminal/ws?${params.toString()}`,
    );

    socket.addEventListener("message", (event) => {
      if (typeof event.data === "string") {
        term.write(event.data);
        return;
      }
      term.write(new Uint8Array(event.data as ArrayBuffer));
    });

    const dataSub = term.onData((data) => {
      if (socket.readyState === WebSocket.OPEN) socket.send(data);
    });
    const resizeSub = term.onResize(({ cols, rows }) => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "resize", cols, rows }));
      }
    });

    const observer = new ResizeObserver(() => {
      try {
        fit.fit();
      } catch {
        /* container may be hidden */
      }
    });
    observer.observe(node);
    requestAnimationFrame(() => {
      try {
        fit.fit();
      } catch {
        /* first layout */
      }
      term.focus();
    });

    return () => {
      observer.disconnect();
      dataSub.dispose();
      resizeSub.dispose();
      socket.close();
      term.dispose();
    };
  }, [workspace, sessionId]);

  if (!workspace) {
    return <p className="p-3 text-[12px] text-muted-foreground">请先选择工作区，再使用终端。</p>;
  }

  return <div ref={host} className="workspace-terminal h-full min-h-0 w-full" />;
}
