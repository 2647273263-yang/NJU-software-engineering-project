import { fenceOf } from "./syntax";

export type ChatRef = {
  id: string;
  path: string;
  kind?: "file" | "image";
  preview?: string;
  startLine?: number;
  endLine?: number;
  snippet?: string;
};

export function isImagePath(path: string): boolean {
  return /\.(png|jpe?g|gif|webp|bmp)$/i.test(path);
}

export function chatRefId(path: string, startLine?: number, endLine?: number): string {
  return `${path}::${startLine ?? ""}-${endLine ?? ""}`;
}

export function makeChatRef(
  path: string,
  extra: {
    startLine?: number;
    endLine?: number;
    snippet?: string;
    kind?: "file" | "image";
    preview?: string;
  } = {},
): ChatRef {
  return {
    id: chatRefId(path, extra.startLine, extra.endLine),
    path,
    kind: extra.kind ?? (isImagePath(path) ? "image" : "file"),
    ...extra,
  };
}

export function formatChatPayload(text: string, refs: ChatRef[]): string {
  const body = text.trim();
  if (refs.length === 0) return body;
  const chunks: string[] = [];
  if (body) chunks.push(body);
  for (const ref of refs) {
    if (ref.kind === "image") {
      chunks.push(`用户附上了截图 \`${ref.path}\`。请查看图中的界面或终端报错来解决问题。`);
    } else if (ref.snippet && ref.startLine != null) {
      const end = ref.endLine ?? ref.startLine;
      const range = ref.startLine === end ? `第 ${ref.startLine} 行` : `第 ${ref.startLine}–${end} 行`;
      chunks.push(
        `关于 \`${ref.path}\` ${range}：\n\`\`\`${fenceOf(ref.path)}\n${ref.snippet.replace(/\n$/, "")}\n\`\`\``,
      );
    } else {
      chunks.push(`请阅读并针对文件 \`${ref.path}\` 回答。`);
    }
  }
  return chunks.join("\n\n");
}

export function imagePathsOf(refs: ChatRef[]): string[] {
  return refs.filter((item) => item.kind === "image").map((item) => item.path);
}

export function imagePathsFromText(text: string): string[] {
  const found = text.match(/\.forge-uploads\/[^\s`]+/g) ?? [];
  return [...new Set(found)];
}
