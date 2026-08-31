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
    <div className="pl-0.5">
      <button
        type="button"
        className="flex w-full items-center gap-2 py-1.5 text-left text-muted-foreground transition-colors duration-150 hover:text-foreground"
        onClick={() => setOpen((value) => !value)}
      >
        <ChevronDown className={cn("h-3.5 w-3.5 shrink-0 transition-transform duration-150", !open && "-rotate-90")} />
        <span className="min-w-0 truncate text-[12px] tracking-wide">{summary}</span>
      </button>
      {open ? (
        <div className="ml-1.5 space-y-2 border-l border-white/[0.08] pl-3">
          {items.map((item, index) => (
            <div
              key={`${item.kind}-${index}-${item.title}`}
              className={cn(
                "py-0.5",
                item.tone === "danger" && "text-red-300/90",
                item.tone === "warning" && "text-amber-300/90",
              )}
            >
              <div className="text-[12.5px] text-foreground/85">{item.title}</div>
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
