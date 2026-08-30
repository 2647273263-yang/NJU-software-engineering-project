const KEYWORDS: Record<string, string[]> = {
  python: [
    "and", "as", "assert", "async", "await", "break", "class", "continue", "def", "del",
    "elif", "else", "except", "False", "finally", "for", "from", "global", "if", "import",
    "in", "is", "lambda", "None", "nonlocal", "not", "or", "pass", "raise", "return",
    "True", "try", "while", "with", "yield",
  ],
  js: [
    "async", "await", "break", "case", "catch", "class", "const", "continue", "debugger",
    "default", "delete", "else", "export", "extends", "false", "finally", "for", "from",
    "function", "if", "import", "in", "instanceof", "let", "new", "null", "of", "return",
    "static", "super", "switch", "this", "throw", "true", "try", "typeof", "undefined",
    "var", "void", "while", "with", "yield", "type", "interface", "enum", "implements",
  ],
  rust: [
    "as", "async", "await", "break", "const", "continue", "crate", "dyn", "else", "enum",
    "extern", "false", "fn", "for", "if", "impl", "in", "let", "loop", "match", "mod",
    "move", "mut", "pub", "ref", "return", "self", "Self", "static", "struct", "super",
    "trait", "true", "type", "unsafe", "use", "where", "while",
  ],
  go: [
    "break", "case", "chan", "const", "continue", "default", "defer", "else", "fallthrough",
    "false", "for", "func", "go", "goto", "if", "import", "interface", "map", "nil",
    "package", "range", "return", "select", "struct", "switch", "true", "type", "var",
  ],
};

const EXT_LANG: Record<string, string> = {
  py: "python",
  pyw: "python",
  js: "js",
  jsx: "js",
  mjs: "js",
  cjs: "js",
  ts: "js",
  tsx: "js",
  json: "json",
  css: "css",
  scss: "css",
  html: "html",
  xml: "html",
  svg: "html",
  md: "md",
  markdown: "md",
  rs: "rust",
  go: "go",
  sh: "sh",
  bash: "sh",
  zsh: "sh",
  ps1: "sh",
  toml: "toml",
  yml: "yaml",
  yaml: "yaml",
  sql: "sql",
  java: "js",
  kt: "js",
  c: "js",
  h: "js",
  cpp: "js",
  hpp: "js",
};

export function languageOf(path: string): string {
  const name = path.replaceAll("\\", "/").split("/").pop() ?? "";
  const ext = name.includes(".") ? name.slice(name.lastIndexOf(".") + 1).toLowerCase() : "";
  return EXT_LANG[ext] ?? "";
}

export function fenceOf(path: string): string {
  const lang = languageOf(path);
  if (lang === "js") {
    const ext = path.split(".").pop()?.toLowerCase() ?? "js";
    if (ext === "ts" || ext === "tsx") return ext;
    if (ext === "jsx") return "jsx";
    return "javascript";
  }
  if (lang === "sh") return "bash";
  return lang || "text";
}

function escapeHtml(text: string): string {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function wrap(kind: string, text: string): string {
  return `<span class="tok-${kind}">${escapeHtml(text)}</span>`;
}

function highlightGeneric(line: string, lang: string): string {
  const keywords = new Set(KEYWORDS[lang] ?? []);
  const comment =
    lang === "python" || lang === "toml" || lang === "yaml" || lang === "sh"
      ? "#"
      : lang === "sql"
        ? "--"
        : "//";
  const out: string[] = [];
  let i = 0;
  const isIdent = (ch: string) => /[A-Za-z_0-9]/.test(ch);

  while (i < line.length) {
    const rest = line.slice(i);
    if (comment === "#" && rest.startsWith("#")) {
      out.push(wrap("cmt", line.slice(i)));
      break;
    }
    if (comment === "--" && rest.startsWith("--")) {
      out.push(wrap("cmt", line.slice(i)));
      break;
    }
    if (comment === "//" && rest.startsWith("//")) {
      out.push(wrap("cmt", line.slice(i)));
      break;
    }
    if (lang === "js" && rest.startsWith("/*")) {
      const end = line.indexOf("*/", i + 2);
      const stop = end < 0 ? line.length : end + 2;
      out.push(wrap("cmt", line.slice(i, stop)));
      i = stop;
      continue;
    }
    const quote = rest[0] === '"' || rest[0] === "'" || rest[0] === "`" ? rest[0] : "";
    if (quote) {
      let j = i + 1;
      while (j < line.length) {
        if (line[j] === "\\") {
          j += 2;
          continue;
        }
        if (line[j] === quote) {
          j += 1;
          break;
        }
        j += 1;
      }
      out.push(wrap("str", line.slice(i, j)));
      i = j;
      continue;
    }
    if (/[0-9]/.test(line[i] ?? "") && (i === 0 || !isIdent(line[i - 1] ?? ""))) {
      let j = i;
      while (j < line.length && /[0-9_.xXa-fA-F]/.test(line[j] ?? "")) j += 1;
      out.push(wrap("num", line.slice(i, j)));
      i = j;
      continue;
    }
    if (/[A-Za-z_]/.test(line[i] ?? "")) {
      let j = i;
      while (j < line.length && isIdent(line[j] ?? "")) j += 1;
      const word = line.slice(i, j);
      const next = line.slice(j).match(/^\s*\(/) ? "fn" : "";
      if (keywords.has(word)) out.push(wrap("kw", word));
      else if (next) out.push(wrap("fn", word));
      else if (/^[A-Z]/.test(word)) out.push(wrap("type", word));
      else out.push(escapeHtml(word));
      i = j;
      continue;
    }
    out.push(escapeHtml(line[i] ?? ""));
    i += 1;
  }
  return out.join("");
}

function highlightJson(line: string): string {
  return line.replace(
    /("(?:\\.|[^"\\])*")(\s*:)?|\b(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b|\b(true|false|null)\b/g,
    (match, str: string, colon?: string, num?: string, kw?: string) => {
      if (str) return wrap(colon ? "kw" : "str", str) + (colon ? escapeHtml(colon) : "");
      if (num) return wrap("num", num);
      if (kw) return wrap("kw", kw);
      return escapeHtml(match);
    },
  );
}

function highlightMarkdown(line: string): string {
  if (/^\s*#/.test(line)) return wrap("kw", line);
  if (/^\s*([-*]|\d+\.)\s/.test(line)) {
    return line.replace(/^(\s*)([-*]|\d+\.)/, (_, space: string, mark: string) => escapeHtml(space) + wrap("kw", mark));
  }
  return escapeHtml(line).replace(
    /`([^`]+)`/g,
    (_match, code: string) => `<span class="tok-str">\`${code}\`</span>`,
  );
}

export function highlightLine(line: string, path: string): string {
  const lang = languageOf(path);
  if (!line) return " ";
  if (lang === "json") return highlightJson(line);
  if (lang === "md") return highlightMarkdown(line);
  if (lang === "html") {
    return escapeHtml(line)
      .replace(/(&lt;\/?[A-Za-z][^&]*&gt;)/g, '<span class="tok-kw">$1</span>')
      .replace(/(&quot;[^&]*&quot;)/g, '<span class="tok-str">$1</span>');
  }
  if (lang === "css") {
    return highlightGeneric(line, "js")
      .replace(/(#[0-9A-Fa-f]{3,8})\b/g, '<span class="tok-num">$1</span>');
  }
  if (lang) return highlightGeneric(line, lang === "toml" || lang === "yaml" ? "python" : lang);
  return escapeHtml(line);
}

export function highlightCode(source: string, path: string): string {
  const lines = source.split("\n");
  return lines.map((line) => highlightLine(line, path) || " ").join("\n");
}
