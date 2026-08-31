import { CheckCircle2, CircleAlert, CircleHelp, FileDiff, GitBranch, ShieldQuestion, TerminalSquare, TestTubes } from "lucide-react";
import type { ClaimItem, ClaimRow } from "../lib/types";
import { cn } from "../lib/utils";

const KIND: Record<string, { label: string; Icon: typeof FileDiff }> = {
  file_change: { label: "改过的文件", Icon: FileDiff },
  command: { label: "跑过的命令", Icon: TestTubes },
  missing_verification: { label: "还没跑检查", Icon: ShieldQuestion },
  failure: { label: "当时失败", Icon: CircleAlert },
  recovery: { label: "后来通过", Icon: CheckCircle2 },
  hypothesis: { label: "排查方向", Icon: CircleHelp },
  git_status: { label: "未提交改动", Icon: GitBranch },
  llm_judge: { label: "评判器", Icon: ShieldQuestion },
};

function kindsOf(claim: ClaimRow): string[] {
  return (claim.items ?? []).map((item) => item.kind).filter(Boolean);
}

function outcome(claim: ClaimRow): {
  label: string;
  className: string;
  Icon: typeof CheckCircle2;
} {
  const kinds = kindsOf(claim);
  if (kinds.includes("file_change")) {
    return {
      label: "文件已写入",
      className: "bg-emerald-500/10 text-emerald-300",
      Icon: FileDiff,
    };
  }
  if (kinds.includes("command") && claim.status === "proven") {
    return {
      label: "检查通过",
      className: "bg-emerald-500/10 text-emerald-300",
      Icon: CheckCircle2,
    };
  }
  if (kinds.includes("command") && claim.status === "unproven") {
    return {
      label: "检查没过",
      className: "bg-amber-500/10 text-amber-300",
      Icon: CircleAlert,
    };
  }
  if (kinds.includes("llm_judge") && claim.status === "proven") {
    return {
      label: "评判器通过",
      className: "bg-emerald-500/10 text-emerald-300",
      Icon: CheckCircle2,
    };
  }
  if (kinds.includes("llm_judge")) {
    return {
      label: "评判器未通过",
      className: "bg-amber-500/10 text-amber-300",
      Icon: ShieldQuestion,
    };
  }
  if (kinds.includes("missing_verification")) {
    return {
      label: "还没跑检查",
      className: "bg-amber-500/10 text-amber-300",
      Icon: ShieldQuestion,
    };
  }
  if (kinds.includes("failure") && kinds.includes("recovery")) {
    return {
      label: "后来测通了",
      className: "bg-emerald-500/10 text-emerald-300",
      Icon: CheckCircle2,
    };
  }
  if (kinds.includes("git_status")) {
    return {
      label: "还有未提交改动",
      className: "bg-white/5 text-muted-foreground",
      Icon: GitBranch,
    };
  }
  if (claim.status === "unverifiable") {
    return {
      label: "没法用命令证明",
      className: "bg-white/5 text-muted-foreground",
      Icon: CircleHelp,
    };
  }
  if (claim.status === "proven") {
    return {
      label: "有实际记录",
      className: "bg-emerald-500/10 text-emerald-300",
      Icon: CheckCircle2,
    };
  }
  return {
    label: "还对不上",
    className: "bg-amber-500/10 text-amber-300",
    Icon: CircleAlert,
  };
}

function humanStatement(claim: ClaimRow): string {
  const statement = claim.statement;
  const changed = statement.match(/^Changed (\d+) file\(s\)$/i);
  if (changed) {
    const count = changed[1];
    return count === "1" ? "这一轮改了 1 个文件，已经写进磁盘。" : `这一轮改了 ${count} 个文件，已经写进磁盘。`;
  }
  const verify = statement.match(/^Verification command `(.+)` passed$/i);
  if (verify) {
    const command = verify[1];
    return claim.status === "proven"
      ? `已经跑过检查：${command}。结果通过。`
      : `已经跑过检查：${command}。结果没有通过。`;
  }
  if (statement === "The latest workspace changes were verified") {
    return "改了文件，但还没有跑测试或检查。对话里如果说「修好了」，这里还不能当真。";
  }
  if (statement === "LLM Judge accepted the run") {
    return "评判器对照原任务看过了，认为可以结束。检查是否通过仍以上面的命令记录为准。";
  }
  if (statement === "LLM Judge blocked stopping") {
    return "评判器认为原任务还没做完，已经拦住结束并让 Agent 继续。";
  }
  if (statement === "Recovered from a failed verification") {
    return "检查先失败过一次，后来又跑通了。";
  }
  if (statement === "Working tree snapshot after the run") {
    return "跑完后工作区还有未提交的改动。要保存或恢复，请用右侧「版本」页。";
  }
  return statement;
}

function humanEvidence(item: ClaimItem): string {
  const raw = item.description.trim();
  const exit = raw.match(/^exit=(-?\d+), duration=(\d+)ms$/i);
  if (exit) {
    const code = exit[1];
    const ms = Number(exit[2]);
    const time = ms >= 1000 ? `${(ms / 1000).toFixed(1)} 秒` : `${ms} 毫秒`;
    if (code === "0") return `命令正常结束，耗时 ${time}。`;
    return `命令失败，退出码 ${code}，耗时 ${time}。`;
  }
  if (raw === "No verification result exists for the latest edit") {
    return "改完之后没有跑测试或检查命令。";
  }
  const git = raw.match(/^(\d+) changed, (\d+) untracked, \+(\d+)\/-(\d+)$/);
  if (git) {
    return `已跟踪改动 ${git[1]} 个，未跟踪 ${git[2]} 个；新增 ${git[3]} 行，删除 ${git[4]} 行。`;
  }
  if (raw === item.reference) return "内容改动见上方更改栏。";
  return raw || "没有更多说明";
}

function kindMeta(kind: string) {
  return KIND[kind] ?? { label: kind || "记录", Icon: TerminalSquare };
}

function isGitInstallNoise(claim: ClaimRow): boolean {
  if (!kindsOf(claim).includes("git_status")) return false;
  if (claim.status !== "proven") return true;
  const blob = [claim.statement, claim.evidence, ...(claim.items ?? []).map((item) => item.description)].join(
    "\n",
  );
  return /git is not installed|not on PATH|git status unavailable/i.test(blob);
}

export function EvidencePanel({ claims }: { claims: ClaimRow[] }) {
  const visible = claims.filter((claim) => !isGitInstallNoise(claim));
  return (
    <div className="h-full overflow-y-auto px-3 py-3">
      <div className="mb-3 space-y-1">
        <h2 className="text-[13px] font-medium">本轮验收</h2>
        <p className="text-[12px] leading-5 text-muted-foreground">
          用来核对 Agent 这一轮实际做了什么：改了哪些文件、有没有跑测试或检查、过没过。复杂任务结束前还会多一层评判器对照原话验收。可以据此决定要不要接受改动，或让它再试一次。存档、分支和恢复请用「版本」。
        </p>
      </div>
      {visible.length === 0 ? (
        <p className="px-1 py-6 text-center text-[12px] text-muted-foreground">
          还没有本轮记录。等 Agent 改完文件或跑完检查，会显示在这里。
        </p>
      ) : (
        <div className="space-y-2">
          {visible.map((claim, index) => {
            const meta = outcome(claim);
            const Icon = meta.Icon;
            const items = claim.items?.length
              ? claim.items
              : [{ kind: "", description: claim.evidence, reference: "" }];
            return (
              <section
                key={`${claim.statement}-${index}`}
                className="rounded-md bg-white/[0.03] px-3 py-2.5"
              >
                <div className="flex items-start gap-2">
                  <span
                    className={cn(
                      "mt-0.5 inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[11px]",
                      meta.className,
                    )}
                  >
                    <Icon className="h-3 w-3" />
                    {meta.label}
                  </span>
                  <p className="min-w-0 flex-1 text-[13px] leading-5">{humanStatement(claim)}</p>
                </div>
                <ul className="mt-2 space-y-1.5 border-t border-white/5 pt-2">
                  {items.map((item, itemIndex) => {
                    const kind = kindMeta(item.kind);
                    const KindIcon = kind.Icon;
                    const looksLikeDiff = /^(--- |\+\+\+ |@@ )/.test(item.description);
                    return (
                      <li key={`${item.kind}-${itemIndex}`} className="flex gap-2 text-[12px]">
                        <KindIcon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                        <div className="min-w-0">
                          <div className="text-[11px] text-muted-foreground">{kind.label}</div>
                          {item.reference ? (
                            <div className="truncate font-mono text-[11px] text-primary/80">{item.reference}</div>
                          ) : null}
                          <pre
                            className={cn(
                              "whitespace-pre-wrap break-all font-sans text-[12px] leading-5 text-foreground/85",
                              looksLikeDiff && "font-mono text-[11px] text-muted-foreground",
                            )}
                          >
                            {looksLikeDiff ? item.description : humanEvidence(item)}
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
