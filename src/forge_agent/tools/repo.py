"""Compact, ignore-aware repository overview tool."""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from forge_agent.tools.filesystem import iter_visible_paths
from forge_agent.tools.schemas import RepoOutlineArgs
from forge_agent.tools.workspace import WorkspaceSandbox
from forge_agent.types import ToolResult

_LANGUAGES = {
    ".c": "C",
    ".cpp": "C++",
    ".cs": "C#",
    ".css": "CSS",
    ".go": "Go",
    ".html": "HTML",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".json": "JSON",
    ".kt": "Kotlin",
    ".md": "Markdown",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".sh": "Shell",
    ".sql": "SQL",
    ".swift": "Swift",
    ".toml": "TOML",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".vue": "Vue",
    ".xml": "XML",
    ".yaml": "YAML",
    ".yml": "YAML",
}
_CONFIG_NAMES = {
    ".editorconfig",
    ".env.example",
    ".gitignore",
    "Cargo.toml",
    "Dockerfile",
    "Makefile",
    "README.md",
    "compose.yaml",
    "docker-compose.yml",
    "go.mod",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "tox.ini",
    "tsconfig.json",
}
_ENTRYPOINT_NAMES = {
    "__main__.py",
    "app.py",
    "cli.py",
    "index.js",
    "index.ts",
    "main.go",
    "main.js",
    "main.py",
    "main.rs",
    "main.ts",
    "manage.py",
}
_JAVASCRIPT_SUFFIXES = {".js", ".jsx", ".ts", ".tsx"}
_JS_DECLARATION_RE = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?"
    r"(?:(?:declare|abstract)\s+)*(?:(async)\s+)?(class|function)\s+([A-Za-z_$][\w$]*)"
)
_JS_NAMED_EXPORT_RE = re.compile(r"^\s*export\s*\{([^}]*)\}")
_JS_VALUE_EXPORT_RE = re.compile(
    r"^\s*export\s+(?:(?:declare|default)\s+)*(const|let|var|interface|type|enum|namespace)"
    r"\s+([A-Za-z_$][\w$]*)"
)
_JS_DEFAULT_EXPORT_RE = re.compile(
    r"^\s*export\s+default\s+([A-Za-z_$][\w$]*)\s*;?\s*$"
)
_QUERY_WORD_RE = re.compile(r"[\w$.-]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class _FileMap:
    path: str
    symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CachedSymbols:
    mtime_ns: int
    size: int
    symbols: tuple[str, ...]


# Shared by RepoTools instances so repeated registry construction can still reuse parsed files.
_SYMBOL_CACHE: dict[tuple[Path, str], _CachedSymbols] = {}


def _strict_truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    marker = "\n… [truncated]"
    if limit <= len(marker):
        return marker[:limit], True
    return text[: limit - len(marker)] + marker, True


def _python_symbols(path: Path, relative: str) -> tuple[str, ...]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return ()
    symbols: list[str] = []
    for node in tree.body:
        kind: str | None = None
        name: str | None = None
        if isinstance(node, ast.ClassDef):
            kind, name = "class", node.name
        elif isinstance(node, ast.AsyncFunctionDef):
            kind, name = "async def", node.name
        elif isinstance(node, ast.FunctionDef):
            kind, name = "def", node.name
        if kind is not None and name is not None:
            symbols.append(f"{relative}:{node.lineno}: {kind} {name}")
    return tuple(symbols)


def _mask_javascript(source: str) -> str:
    """Blank comments and string contents while preserving braces and line numbers."""
    result: list[str] = []
    index = 0
    state = "code"
    quote = ""
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if state == "code":
            if char == "/" and following == "/":
                result.extend((" ", " "))
                index += 2
                state = "line_comment"
                continue
            if char == "/" and following == "*":
                result.extend((" ", " "))
                index += 2
                state = "block_comment"
                continue
            if char in {"'", '"', "`"}:
                quote = char
                result.append(" ")
                state = "string"
            else:
                result.append(char)
        elif state == "line_comment":
            result.append("\n" if char == "\n" else " ")
            if char == "\n":
                state = "code"
        elif state == "block_comment":
            result.append("\n" if char == "\n" else " ")
            if char == "*" and following == "/":
                result.append(" ")
                index += 1
                state = "code"
        else:
            result.append("\n" if char == "\n" else " ")
            if char == "\\":
                if following:
                    result.append("\n" if following == "\n" else " ")
                    index += 1
            elif char == quote:
                state = "code"
        index += 1
    return "".join(result)


def _javascript_symbols(path: Path, relative: str) -> tuple[str, ...]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()

    symbols: list[str] = []
    depth = 0
    for line_number, line in enumerate(_mask_javascript(source).splitlines(), 1):
        if depth == 0:
            declaration = _JS_DECLARATION_RE.match(line)
            if declaration:
                async_prefix, kind, name = declaration.groups()
                label = f"async {kind}" if async_prefix else kind
                symbols.append(f"{relative}:{line_number}: {label} {name}")
            else:
                value_export = _JS_VALUE_EXPORT_RE.match(line)
                default_export = _JS_DEFAULT_EXPORT_RE.match(line)
                named_export = _JS_NAMED_EXPORT_RE.match(line)
                if value_export:
                    kind, name = value_export.groups()
                    symbols.append(f"{relative}:{line_number}: export {kind} {name}")
                elif default_export:
                    symbols.append(
                        f"{relative}:{line_number}: export default {default_export.group(1)}"
                    )
                elif named_export:
                    for item in named_export.group(1).split(","):
                        exported = item.strip().split()
                        if exported:
                            name = (
                                exported[-1]
                                if len(exported) >= 3 and exported[-2] == "as"
                                else exported[0]
                            )
                            symbols.append(f"{relative}:{line_number}: export {name}")
        depth = max(0, depth + line.count("{") - line.count("}"))
    return tuple(symbols)


def _cached_symbols(path: Path, relative: str) -> tuple[str, ...]:
    try:
        stat = path.stat()
    except OSError:
        return ()
    cache_key = (path, relative)
    cached = _SYMBOL_CACHE.get(cache_key)
    if cached is not None and (cached.mtime_ns, cached.size) == (stat.st_mtime_ns, stat.st_size):
        return cached.symbols
    suffix = path.suffix.lower()
    if suffix == ".py":
        symbols = _python_symbols(path, relative)
    elif suffix in _JAVASCRIPT_SUFFIXES:
        symbols = _javascript_symbols(path, relative)
    else:
        symbols = ()
    _SYMBOL_CACHE[cache_key] = _CachedSymbols(stat.st_mtime_ns, stat.st_size, symbols)
    return symbols


def _relevance(file_map: _FileMap, search: str) -> int:
    if not search.strip():
        return 0
    query = search.casefold()
    path = file_map.path.casefold()
    filename = Path(file_map.path).name.casefold()
    symbol_text = " ".join(file_map.symbols).casefold()
    score = 0
    if query in filename:
        score += 120
    elif query in path:
        score += 80
    if query in symbol_text:
        score += 100
    for word in set(_QUERY_WORD_RE.findall(query)):
        if len(word) < 2:
            continue
        if word == filename or word == Path(filename).stem:
            score += 50
        elif word in path:
            score += 20
        if word in symbol_text:
            score += 35
    return score


class RepoTools:
    def __init__(self, sandbox: WorkspaceSandbox, *, max_output_chars: int = 20_000) -> None:
        self.sandbox = sandbox
        self.max_output_chars = max_output_chars

    def repo_outline(self, args: RepoOutlineArgs) -> ToolResult:
        base = self.sandbox.resolve(args.path, must_exist=True)
        if not base.is_dir():
            raise NotADirectoryError(args.path)

        files = [
            path
            for path in iter_visible_paths(
                self.sandbox,
                base,
                recursive=True,
                include_directories=False,
            )
            if path.is_file()
        ]
        languages: Counter[str] = Counter()
        configurations: list[str] = []
        entrypoints: list[str] = []
        test_directories: set[str] = set()
        file_maps: list[_FileMap] = []

        for path in files:
            relative = self.sandbox.relative(path)
            language = _LANGUAGES.get(path.suffix.lower(), "Other")
            languages[language] += 1
            if path.name in _CONFIG_NAMES:
                configurations.append(relative)
            if path.name in _ENTRYPOINT_NAMES:
                entrypoints.append(relative)
            for parent in path.parents:
                if parent == base.parent:
                    break
                if parent.name in {"test", "tests"}:
                    test_directories.add(self.sandbox.relative(parent) + "/")
            file_maps.append(_FileMap(relative, _cached_symbols(path, relative)))

        search = " ".join(part for part in (args.query, args.task) if part)
        ranked_maps = sorted(
            file_maps,
            key=lambda item: (-_relevance(item, search), item.path.casefold()),
        )
        symbols = [symbol for item in ranked_maps for symbol in item.symbols]
        relevant_files = [
            item.path for item in ranked_maps if search and _relevance(item, search) > 0
        ][:20]

        sections = [
            "# Repository outline",
            f"Root: {self.sandbox.relative(base) or '.'}",
            f"Files: {len(files)}",
            "",
            "## Languages",
            *(
                f"- {language}: {count}"
                for language, count in sorted(
                    languages.items(), key=lambda item: (-item[1], item[0])
                )
            ),
            "",
            "## Key configuration",
            *(f"- {path}" for path in sorted(configurations)),
            "",
            "## Entrypoints",
            *(f"- {path}" for path in sorted(entrypoints)),
            "",
            "## Test directories",
            *(f"- {path}" for path in sorted(test_directories)),
            "",
            "## Relevant files",
            *(f"- {path}" for path in relevant_files),
            "",
            "## Top-level symbols",
            *(f"- {symbol}" for symbol in symbols),
        ]
        content = "\n".join(sections)
        budget = min(args.max_chars, self.max_output_chars)
        content, truncated = _strict_truncate(content, budget)
        return ToolResult(
            ok=True,
            summary=f"outlined {len(files)} files",
            content=content,
            truncated=truncated,
            metadata={
                "file_count": len(files),
                "language_count": len(languages),
                "output_budget": budget,
                "relevant_file_count": len(relevant_files),
            },
        )
