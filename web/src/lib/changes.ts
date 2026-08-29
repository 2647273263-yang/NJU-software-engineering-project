import type { SessionEvent } from "./types";

export type ChangeKind = "added" | "deleted" | "modified";

export type ChangedFile = {
  path: string;
  diff: string | null;
  fingerprint: string;
  sha256: string;
  kind: ChangeKind;
  added: number;
  deleted: number;
};

const FILE_EDIT_TOOLS = new Set([
  "write_file",
  "replace_in_file",
  "delete_file",
  "undo_last_edit",
  "rollback_changes",
]);

function diffStats(diff: string): { added: number; deleted: number } {
  let added = 0;
  let deleted = 0;
  for (const raw of diff.split("\n")) {
    if (raw.startsWith("+++") || raw.startsWith("---")) continue;
    if (raw.startsWith("+")) added += 1;
    else if (raw.startsWith("-")) deleted += 1;
  }
  return { added, deleted };
}

function headerPath(diff: string, prefix: "+++" | "---"): string | null {
  const needle = `${prefix} `;
  for (const raw of diff.split("\n")) {
    if (!raw.startsWith(needle)) continue;
    let value = raw.slice(needle.length).replace(/\t.*$/, "").trim().replaceAll("\\", "/");
    if (value.startsWith("a/") || value.startsWith("b/")) value = value.slice(2);
    if (!value || value === "/dev/null") return null;
    return value;
  }
  return null;
}

function payloadMetadata(payload: Record<string, unknown>): Record<string, unknown> | null {
  const metadata = payload.metadata;
  if (typeof metadata === "string") {
    try {
      const parsed = JSON.parse(metadata) as unknown;
      return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
    } catch {
      return null;
    }
  }
  if (!metadata || typeof metadata !== "object") return null;
  return metadata as Record<string, unknown>;
}

function payloadEditTx(payload: Record<string, unknown>): string {
  const metadata = payloadMetadata(payload);
  if (!metadata) return "";
  const id = metadata.edit_transaction_id;
  if (typeof id === "number" && Number.isFinite(id)) return String(id);
  if (typeof id === "string" && id.trim()) return id.trim();
  return "";
}

function eventPath(
  payload: Record<string, unknown>,
  viewPath: string | null | undefined,
  diff: string | null,
): string | null {
  const metadata = payloadMetadata(payload);
  const changed = metadata?.changed_files;
  if (Array.isArray(changed) && typeof changed[0] === "string" && changed[0].trim()) {
    return changed[0].replaceAll("\\", "/");
  }
  const args = payload.arguments;
  if (args && typeof args === "object") {
    const path = (args as { path?: unknown }).path;
    if (typeof path === "string" && path.trim()) return path.replaceAll("\\", "/");
  }
  const plus = diff ? headerPath(diff, "+++") : null;
  const minus = diff ? headerPath(diff, "---") : null;
  return plus ?? minus ?? viewPath?.replaceAll("\\", "/") ?? null;
}

function changeKind(
  payload: Record<string, unknown>,
  name: string,
  diff: string | null,
): ChangeKind {
  const metadata = payloadMetadata(payload);
  if (metadata?.deleted === true || name === "delete_file") return "deleted";
  if (metadata?.created === true) return "added";
  if (diff) {
    const plus = headerPath(diff, "+++");
    const minus = headerPath(diff, "---");
    if (plus && !minus) return "added";
    if (minus && !plus) return "deleted";
    if (/@@ -0,0 \+\d/.test(diff)) return "added";
  }
  return "modified";
}

export function fileFingerprint(path: string, diff: string | null, sha256 = "", tx = ""): string {
  const normalized = path.replaceAll("\\", "/");
  if (tx) return `${normalized}::tx${tx}`;
  if (sha256) return `${normalized}::${sha256.toLowerCase()}`;
  return `${normalized}::${diff ?? ""}`;
}

export function payloadSha256(payload: Record<string, unknown>): string {
  const metadata = payloadMetadata(payload);
  if (!metadata) return "";
  const sha = metadata.sha256;
  return typeof sha === "string" ? sha.toLowerCase() : "";
}

export function collectChangedFiles(events: SessionEvent[]): ChangedFile[] {
  const map = new Map<string, ChangedFile>();
  for (const event of events) {
    if (event?.kind !== "tool_finished" || !event.view) continue;
    const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
    const name = typeof payload.name === "string" ? payload.name : "";
    if (!FILE_EDIT_TOOLS.has(name)) continue;
    if (payload.ok === false) continue;
    const diff = typeof event.view.diff === "string" && event.view.diff ? event.view.diff : null;
    const path = eventPath(payload, event.view.path, diff);
    if (!path) continue;
    const kind = changeKind(payload, name, diff);
    const stats = diff ? diffStats(diff) : { added: 0, deleted: 0 };
    if (kind === "modified" && stats.added === 0 && stats.deleted === 0 && !diff) continue;
    const sha256 = payloadSha256(payload);
    const tx = payloadEditTx(payload);
    map.set(path, {
      path,
      diff,
      fingerprint: fileFingerprint(path, diff, sha256, tx),
      sha256,
      kind,
      ...stats,
    });
  }
  return [...map.values()];
}

export function isChangeAccepted(file: ChangedFile, resolved: Record<string, string>): boolean {
  const stored = resolved[file.path];
  if (typeof stored !== "string" || !stored) return false;
  if (stored === file.fingerprint) return true;
  const token = stored.includes("::") ? stored.slice(stored.lastIndexOf("::") + 2) : stored;
  if (file.diff && (stored === file.diff || token === file.diff)) return true;
  const usesTx = file.fingerprint.includes("::tx");
  if (!usesTx && file.sha256 && token.toLowerCase() === file.sha256) return true;
  return false;
}
