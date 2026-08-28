import { useState } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "../lib/utils";
import type { TimelineView } from "../lib/types";

export function ProcessGroup({ items, openDefault }: { items: TimelineView[]; openDefault: boolean }) {
  const [open, setOpen] = useState(openDefault);
  const headlines = items
    .filter((item) => item.kind === "tool_finished" || item.kind === "automatic_verification_finished")
    .map((item) => item.title);
  const summary =
    headlines.length > 0
      ? headlines.slice(0, 3).join(" · ") + (headlines.length > 3 ? ` 等 ${headlines.length} 项` : "")
      : `${items.length} 个步骤`;

  return (
    <div className="rounded-lg border border-border/80 bg-white/[0.02]">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-muted-foreground hover:text-foreground"
        onClick={() => setOpen((value) => !value)}
      >
        <ChevronDown className={cn("h-3.5 w-3.5 shrink-0 transition", !open && "-rotate-90")} />
        <span className="min-w-0 truncate text-[12.5px]">{summary}</span>
      </button>
      {open ? (
        <div className="space-y-1 border-t border-border/80 px-2 py-2">
          {items.map((item, index) => (
            <div
              key={`${item.kind}-${index}-${item.title}`}
              className={cn(
                "rounded-md px-2 py-1.5",
                item.tone === "danger" && "bg-red-500/10",
                item.tone === "success" && "bg-emerald-500/5",
                item.tone === "warning" && "bg-amber-500/5",
              )}
            >
              <div className="text-[12.5px] text-foreground/90">{item.title}</div>
              {item.detail ? (
                <div className="mt-0.5 max-h-32 overflow-auto whitespace-pre-wrap break-all text-[12px] text-muted-foreground">
                  {item.detail}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
