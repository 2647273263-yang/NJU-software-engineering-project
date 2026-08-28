export type ChangeBlock = {
  id: string;
  newStart: number;
  added: string[];
  deleted: string[];
};

export function parseChangeBlocks(diff: string | null): ChangeBlock[] {
  if (!diff) return [];
  const blocks: ChangeBlock[] = [];
  let newLine = 0;
  let nextId = 0;
  let current: { newStart: number; added: string[]; deleted: string[] } | null = null;

  const flush = () => {
    if (!current) return;
    if (current.added.length > 0 || current.deleted.length > 0) {
      blocks.push({ id: `h${nextId}`, ...current });
      nextId += 1;
    }
    current = null;
  };

  for (const raw of diff.split("\n")) {
    const header = raw.match(/^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (header) {
      flush();
      newLine = Number(header[1]);
      continue;
    }
    if (
      raw.startsWith("+++") ||
      raw.startsWith("---") ||
      raw.startsWith("diff ") ||
      raw.startsWith("index ")
    ) {
      continue;
    }
    if (raw.startsWith("+")) {
      if (!current) current = { newStart: newLine, added: [], deleted: [] };
      current.added.push(raw.slice(1));
      newLine += 1;
      continue;
    }
    if (raw.startsWith("-")) {
      if (!current) current = { newStart: newLine, added: [], deleted: [] };
      current.deleted.push(raw.slice(1));
      continue;
    }
    if (raw.startsWith("\\")) continue;
    flush();
    if (raw.startsWith(" ") || raw === "") newLine += 1;
  }
  flush();
  return blocks;
}

export function applyHunkUndo(value: string, block: ChangeBlock): string {
  const lines = value.split("\n");
  const start = Math.max(0, block.newStart - 1);
  lines.splice(start, block.added.length, ...block.deleted);
  return lines.join("\n");
}

export function dropBlock(blocks: ChangeBlock[], id: string): ChangeBlock[] {
  const target = blocks.find((item) => item.id === id);
  if (!target) return blocks;
  const delta = target.deleted.length - target.added.length;
  return blocks
    .filter((item) => item.id !== id)
    .map((item) =>
      item.newStart > target.newStart ? { ...item, newStart: item.newStart + delta } : item,
    );
}

export function hunkStats(blocks: ChangeBlock[]): { added: number; deleted: number } {
  return blocks.reduce(
    (sum, block) => ({
      added: sum.added + block.added.length,
      deleted: sum.deleted + block.deleted.length,
    }),
    { added: 0, deleted: 0 },
  );
}
