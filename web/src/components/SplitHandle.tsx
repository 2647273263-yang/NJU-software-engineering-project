import { ArrowLeftRight } from "lucide-react";
import { cn } from "../lib/utils";

export function SplitHandle({
  axis = "x",
  onMove,
  onSwap,
}: {
  axis?: "x" | "y";
  onMove: (client: number) => void;
  onSwap?: () => void;
}) {
  const vertical = axis === "x";
  return (
    <div
      className={cn(
        "group relative z-10 shrink-0 bg-transparent hover:bg-white/20 before:absolute before:content-['']",
        vertical
          ? "w-1 cursor-col-resize before:inset-y-0 before:-left-1.5 before:-right-1.5"
          : "h-1 cursor-row-resize before:inset-x-0 before:-top-1.5 before:-bottom-1.5",
      )}
      title={
        onSwap
          ? "拖动调整宽度，双击对调对话与检查器"
          : vertical
            ? "拖动调整宽度"
            : "拖动调整高度"
      }
      onMouseDown={(event) => {
        event.preventDefault();
        document.body.style.cursor = vertical ? "col-resize" : "row-resize";
        document.body.style.userSelect = "none";
        const move = (next: MouseEvent) => onMove(vertical ? next.clientX : next.clientY);
        const up = () => {
          document.body.style.cursor = "";
          document.body.style.userSelect = "";
          window.removeEventListener("mousemove", move);
          window.removeEventListener("mouseup", up);
        };
        window.addEventListener("mousemove", move);
        window.addEventListener("mouseup", up);
      }}
      onDoubleClick={onSwap}
    >
      {onSwap ? (
        <button
          type="button"
          className="absolute top-1/2 left-1/2 z-20 hidden -translate-x-1/2 -translate-y-1/2 rounded-sm bg-popover/90 p-0.5 text-muted-foreground hover:text-foreground group-hover:block"
          title="对调对话区和检查器"
          onMouseDown={(event) => event.stopPropagation()}
          onClick={(event) => {
            event.stopPropagation();
            onSwap();
          }}
        >
          <ArrowLeftRight className="h-3 w-3" />
        </button>
      ) : null}
    </div>
  );
}
