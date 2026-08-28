import { CheckCircle2, CircleAlert, CircleHelp, FileDiff, GitBranch, ShieldQuestion, TerminalSquare, TestTubes } from "lucide-react";
import type { ClaimRow } from "../lib/types";
import { cn } from "../lib/utils";

const STATUS: Record<
  string,
  { label: string; hint: string; className: string; Icon: typeof CheckCircle2 }
> = {
  proven: {
    label: "已证实",
    hint: "有实际改动或命令结果撑腰",
    className: "border-emerald-500/20 bg-emerald-500/10 text-emerald-300",
    Icon: CheckCircle2,
  },
  unproven: {
    label: "未证实",
    hint: "还缺一次成功的验证",
    className: "border-amber-500/20 bg-amber-500/10 text-amber-300",
    Icon: CircleAlert,
  },
  unverifiable: {
    label: "无法验证",
    hint: "这类结论没法用测试命令证明",
    className: "border-white/10 bg-white/5 text-muted-foreground",
    Icon: CircleHelp,
  },
};

const KIND: Record<string, { label: string; Icon: typeof FileDiff }> = {
  file_change: { label: "文件改动", Icon: FileDiff },
  command: { label: "验证命令", Icon: TestTubes },
  missing_verification: { label: "缺少验证", Icon: ShieldQuestion },
  failure: { label: "失败记录", Icon: CircleAlert },
  recovery: { label: "后来通过", Icon: CheckCircle2 },
  hypothesis: { label: "排查假设", Icon: CircleHelp },
  git_status: { label: "工作区状态", Icon: GitBranch },
};

function humanStatement(statement: string, status: string): string {
  const changed = statement.match(/^Changed (\d+) file\(s\)$/i);
  if (changed) return `这次一共改了 ${changed[1]} 个文件`;
  const verify = statement.match(/^Verification command `(.+)` passed$/i);
  if (verify) {
    return status === "proven"
      ? `验证命令已经跑过，并且通过了`
      : `验证命令没有通过`;
  }
  if (statement === "The latest workspace changes were verified") {
    return "改了代码，但还没有跑过验证命令";
  }
  if (statement === "Recovered from a failed verification") {
    return "验证曾经失败，后来又跑通了";
  }
  if (statement === "Working tree snapshot after the run") {
    return "跑完后工作区还留下哪些改动";
  }
  return statement;
}

function kindMeta(kind: string) {
  return KIND[kind] ?? { label: kind || "记录", Icon: TerminalSquare };
}

export function EvidencePanel({ claims }: { claims: ClaimRow[] }) {
  return (
    <div className="h-full overflow-y-auto px-3 py-3">
      <div className="mb-3 space-y-1">
        <h2 className="text-[13px] font-medium">核对结论</h2>
        <p className="text-[12px] leading-5 text-muted-foreground">
          对话里 Agent 可以说“已经修好了”，这里只认实际痕迹：改了哪些文件、测试或检查有没有真的跑过、结果是成功还是失败。
        </p>
      </div>
      {claims.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border px-3 py-6 text-center text-[12px] text-muted-foreground">
          跑完一轮任务后，这里会列出可核对的结论。现在还没有记录。
        </p>
      ) : (
        <div className="space-y-2">
          {claims.map((claim, index) => {
            const meta = STATUS[claim.status] ?? STATUS.unproven;
            const Icon = meta.Icon;
            const items = claim.items?.length
              ? claim.items
              : [{ kind: "", description: claim.evidence, reference: "" }];
            return (
              <section
                key={`${claim.statement}-${index}`}
                className="rounded-lg border border-border bg-[#161616] px-3 py-2.5"
              >
                <div className="flex items-start gap-2">
                  <span
                    className={cn(
                      "mt-0.5 inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[11px]",
                      meta.className,
                    )}
                  >
                    <Icon className="h-3 w-3" />
                    {meta.label}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-[13px] leading-5">{humanStatement(claim.statement, claim.status)}</p>
                    <p className="mt-0.5 text-[11px] text-muted-foreground">{meta.hint}</p>
                  </div>
                </div>
                <ul className="mt-2 space-y-1.5 border-t border-white/5 pt-2">
                  {items.map((item, itemIndex) => {
                    const kind = kindMeta(item.kind);
                    const KindIcon = kind.Icon;
                    return (
                      <li key={`${item.kind}-${itemIndex}`} className="flex gap-2 text-[12px]">
                        <KindIcon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                        <div className="min-w-0">
                          <div className="text-[11px] text-muted-foreground">{kind.label}</div>
                          {item.reference ? (
                            <div className="truncate font-mono text-[11px] text-primary/80">{item.reference}</div>
                          ) : null}
                          <pre className="whitespace-pre-wrap break-all font-sans text-[12px] leading-5 text-foreground/85">
                            {item.description || "没有更多说明"}
                          </pre>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
