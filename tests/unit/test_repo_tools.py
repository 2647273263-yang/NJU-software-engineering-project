from __future__ import annotations

from pathlib import Path

import pytest

from forge_agent.tools import build_default_registry
from forge_agent.tools import repo as repo_module


@pytest.mark.asyncio
async def test_read_file_ranges_line_numbers_limit_and_binary(tmp_path: Path) -> None:
    text = "\n".join(f"line {number}" for number in range(1, 302))
    (tmp_path / "long.txt").write_text(text, encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"text\0binary")
    registry = build_default_registry(tmp_path)

    ranged = await registry.call(
        "read_file",
        {"path": "long.txt", "start_line": 2, "end_line": 3},
    )
    assert ranged.ok
    assert ranged.content == "2 | line 2\n3 | line 3"
    assert not ranged.truncated
    assert ranged.metadata["total_lines"] == 301

    default = await registry.call("read_file", {"path": "long.txt"})
    assert default.ok
    assert len(default.content.splitlines()) == 300
    assert default.content.splitlines()[-1].endswith("line 300")
    assert default.truncated

    binary = await registry.call("read_file", {"path": "binary.bin"})
    assert not binary.ok
    assert binary.error_code == "ValueError"
    assert "binary" in binary.summary


@pytest.mark.asyncio
async def test_list_and_python_search_respect_ignore_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".gitignore").write_text("ignored.txt\nignored-dir/\n", encoding="utf-8")
    (tmp_path / "kept.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "ignored-dir").mkdir()
    (tmp_path / "ignored-dir" / "hidden.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dependency.js").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr("forge_agent.tools.filesystem.shutil.which", lambda _: None)
    registry = build_default_registry(tmp_path)

    listing = await registry.call("list_files", {"recursive": True})
    assert listing.ok
    assert "kept.py" in listing.content
    assert "ignored.txt" not in listing.content
    assert "ignored-dir" not in listing.content
    assert "node_modules" not in listing.content

    search = await registry.call("search_text", {"query": "needle"})
    assert search.ok
    assert search.metadata["count"] == 1
    assert search.content == "kept.py:1:needle"


@pytest.mark.asyncio
async def test_repo_outline_reports_structure_symbols_and_strict_budget(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "class Greeter:\n    pass\n\nasync def serve():\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text(
        "def test_greeting():\n    pass\n",
        encoding="utf-8",
    )
    registry = build_default_registry(tmp_path)

    outline = await registry.call("repo_outline", {"max_chars": 2_000})
    assert outline.ok
    assert "Python: 2" in outline.content
    assert "pyproject.toml" in outline.content
    assert "src/main.py" in outline.content
    assert "tests/" in outline.content
    assert "class Greeter" in outline.content
    assert "async def serve" in outline.content

    bounded = await registry.call("repo_outline", {"max_chars": 200})
    assert bounded.ok
    assert bounded.truncated
    assert len(bounded.content) <= 200


@pytest.mark.asyncio
async def test_repo_outline_parses_javascript_typescript_and_ranks_query(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "unrelated.js").write_text(
        "export function helper() {}\nclass Internal {}\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "payments.ts").write_text(
        """
// export function Fake() {}
export interface Invoice {
  total: number
}
export default class PaymentService {
  method() {
    function nested() {}
  }
}
export async function chargeCard() {}
export const paymentVersion = 1
export { Invoice as BillingInvoice }
""".strip(),
        encoding="utf-8",
    )
    registry = build_default_registry(tmp_path)

    outline = await registry.call(
        "repo_outline",
        {"query": "chargeCard", "task": "payments", "max_chars": 4_000},
    )

    assert outline.ok
    assert "TypeScript: 1" in outline.content
    assert "src/payments.ts:2: export interface Invoice" in outline.content
    assert "src/payments.ts:5: class PaymentService" in outline.content
    assert "src/payments.ts:10: async function chargeCard" in outline.content
    assert "src/payments.ts:11: export const paymentVersion" in outline.content
    assert "src/payments.ts:12: export BillingInvoice" in outline.content
    assert "Fake" not in outline.content
    assert "nested" not in outline.content
    relevant = outline.content.split("## Relevant files", 1)[1].split("## Top-level symbols", 1)[0]
    assert relevant.strip().splitlines()[0] == "- src/payments.ts"
    assert outline.metadata["relevant_file_count"] == 1


@pytest.mark.asyncio
async def test_repo_outline_symbol_cache_uses_stat_and_invalidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "index.ts"
    source.write_text("export function first() {}\n", encoding="utf-8")
    calls = 0
    original = repo_module._javascript_symbols

    def counting_parser(path: Path, relative: str) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return original(path, relative)

    monkeypatch.setattr(repo_module, "_javascript_symbols", counting_parser)
    registry = build_default_registry(tmp_path)

    first = await registry.call("repo_outline", {})
    cached = await registry.call("repo_outline", {})
    assert first.ok and cached.ok
    assert calls == 1

    source.write_text("export function secondLongerName() {}\n", encoding="utf-8")
    changed = await registry.call("repo_outline", {})
    assert changed.ok
    assert calls == 2
    assert "secondLongerName" in changed.content
    assert "function first" not in changed.content
